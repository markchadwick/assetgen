#! /usr/bin/env python

# Public Domain (-) 2010-2011 The Assetgen Authors.
# See the Assetgen UNLICENSE file for details.

"""Asset generator for modern web app development."""

import os
import sys

from base64 import b64encode
from fnmatch import fnmatch
from hashlib import sha1
from mimetypes import guess_type
from optparse import OptionParser
from os import chdir, environ, makedirs, remove, stat, walk
from os.path import dirname, isfile, isdir, join, realpath, split, splitext
from re import compile as compile_regex
from subprocess import PIPE, Popen
from shutil import rmtree
from stat import ST_MTIME
from tempfile import gettempdir
from time import sleep

try:
    from cPickle import dump, load
except ImportError:
    from pickle import dump, load

from simplejson import dump as encode_json
from tavutil.env import run_command
from tavutil.optcomplete import autocomplete
from tavutil.scm import is_git, SCMConfig
from yaml import safe_load as decode_yaml

# ------------------------------------------------------------------------------
# Some Globals
# ------------------------------------------------------------------------------

DEBUG = False
HANDLERS = {}
LOCKS = {}

# ------------------------------------------------------------------------------
# Default Settings
# ------------------------------------------------------------------------------

DEFAULTS = {
    'css.bidi.extension': '.rtl',
    'css.compressed': True,
    'css.embed.extension': '.data',
    'css.embed.url.template': "%(url_base)s%(prefix)s/%(hash)s%(filename)s",
    'js.compressed': True,
    'output.template': '%(hash)s-%(filename)s'
    }

# ------------------------------------------------------------------------------
# Lock Support
# ------------------------------------------------------------------------------

def lock(path, config_path):
    LOCKS[path] = lock = open(path, 'w')
    try:
        from fcntl import flock, LOCK_EX, LOCK_NB
    except ImportError:
        exit("Locking is not supported on this platform.")
    try:
        flock(lock.fileno(), LOCK_EX | LOCK_NB)
    except Exception:
        exit("Another assetgen is already running for %s." % config_path)

def unlock(path):
    if path in LOCKS:
        LOCKS[path].close()
        del LOCKS[path]

# ------------------------------------------------------------------------------
# Utility Functions
# ------------------------------------------------------------------------------

register_handler = HANDLERS.__setitem__

def read(filename):
    if isinstance(filename, Raw):
        return filename.text
    file = open(filename, 'rb')
    content = file.read()
    file.close()
    return content

def newer(input, output, cache):
    if input in cache:
        input_mtime = cache[input]
    else:
        input_mtime = stat(input)[ST_MTIME]
    if output in cache:
        output_mtime = cache[output]
    else:
        try:
            output_mtime = stat(output)[ST_MTIME]
        except Exception:
            return 1
    if input_mtime >= output_mtime:
        return 1

def do(args, **kwargs):
    kwargs['exit_on_error'] = 1
    kwargs['retcode'] = 0
    kwargs['redirect_stdout'] = 1
    kwargs['redirect_stderr'] = 0
    return run_command(args, **kwargs)

def stderr(*args, **kwargs):
    kwargs['reterror'] = 1
    return run_command(args, **kwargs)[1]

def exit(msg):
    print "ERROR:", msg
    sys.exit(1)

# ------------------------------------------------------------------------------
# Raw Text Class
# ------------------------------------------------------------------------------

class Raw(object):
    """Raw text container class."""

    def __init__(self, text):
        self.text = text

# ------------------------------------------------------------------------------
# Base Asset Class
# ------------------------------------------------------------------------------

class Asset(object):
    """Base generator class for Assets."""

    def __init__(self, runner, path, sources, depends, spec):
        self.runner = runner
        self.path = path
        self.sources = sources
        self.depends = depends
        self.spec = spec

    def __str__(self):
        return "<%s: %s>" % (self.__class__.__name__, self.path)

    __repr__ = __str__

    def emit(self, path, content, extension=''):
        self.runner.emit(self.path, path, content, extension)

    def is_fresh(self):
        return self.runner.is_fresh(self.path, self.depends)

    def generate(self):
        exit("No %s.generate() method implemented." % self.__class__.__name__)

# ------------------------------------------------------------------------------
# Binary Assets
# ------------------------------------------------------------------------------

class BinaryAsset(Asset):
    """Generator for Binary Assets."""

    def generate(self):
        self.emit(
            self.path, ''.join(read(source) for source in self.sources)
            )

register_handler('binary',  BinaryAsset)
register_handler('html',    BinaryAsset)
register_handler('png',     BinaryAsset)


# ------------------------------------------------------------------------------
# CSS Assets
# ------------------------------------------------------------------------------

embed_regex = compile_regex(r'embed\("([^\)]*)"\)')
find_embeds = embed_regex.findall
substitute_embeds = embed_regex.sub

class CSSAsset(Asset):
    """Generator for CSS Assets."""

    def __init__(self, *args):
        super(CSSAsset, self).__init__(*args)
        get_spec = self.spec.get
        self.cache = {}
        self.embed_path_root = get_spec('embed.path.root')
        self.embed_url_base = get_spec('embed.url.base')
        self.embed_url_template = get_spec('embed.url.template')
        self.todo = (
            get_spec('bidi') and ('', get_spec('bidi.extension'))  or ('',)
            )

    def convert_to_data_uri(self, match):
        path = match.group(1)
        ctype = guess_type(path)[0]
        if not ctype:
            exit("Could not detect the content type of: %s" % path)
        data, ok = self.get_embed_file(path)
        if not ok:
            return data
        content = b64encode(data)
        if len(content) > 32000:
            return 'url("%s")' % self.get_embed_url(path, data)
        return 'url("data:%s;base64,%s")' % (ctype, content)

    def convert_to_url(self, match):
        path = match.group(1)
        data, ok = self.get_embed_file(path)
        if not ok:
            return data
        return 'url("%s")' % self.get_embed_url(path, data)

    def get_embed_file(self, path):
        if not self.first:
            return self.cache[path]
        try:
            data = open(join(self.embed_path_root, path), 'rb').read()
        except IOError:
            print "!! Couldn't find %s for %s" % (
                join(self.embed_path_root, path), self.path
                )
            return self.cache.setdefault(
                path,
                ('url("%s")' % self.get_embed_url(path), 0)
                )
        return self.cache.setdefault(path, (data, 1))

    def get_embed_url(self, path, data=None):
        if data is None:
            digest = ''
        else:
            digest = sha1(data).hexdigest() + '-'
        prefix, filename = split(path)
        return self.embed_url_template % {
            'url_base': self.embed_url_base,
            'prefix': prefix,
            'hash': digest,
            'filename': filename,
            }

    def embed(self, replacer, content):
        output = substitute_embeds(replacer, content)
        self.first = 0
        return output

    def generate(self):
        get_spec = self.spec.get
        self.first = 1
        self.cache.clear()
        for bidi in self.todo:
            output = []; out = output.append
            for source in self.sources:
                if isinstance(source, Raw):
                    out(source.text)
                elif source.endswith('.sass'):
                    cmd = ['sass', '--scss']
                    if bidi:
                        cmd.append('--flip')
                    if get_spec('compressed'):
                        cmd.extend(['--style', 'compressed'])
                    cmd.append(source)
                    out(do(cmd))

                elif source.endswith('.less'):
                    cmd = ['lessc']
                    cmd.append(source)
                    out(do(cmd))

                else:
                    [out(l) for l in open(source).readlines()]
            output = ''.join(output)
            if self.embed_path_root and self.embed_url_base:
                self.emit(
                    self.path,
                    self.embed(self.convert_to_data_uri, output),
                    get_spec('embed.extension') + bidi
                    )
            self.emit(
                self.path, self.embed(self.convert_to_url, output), bidi
                )

register_handler('css', CSSAsset)

# ------------------------------------------------------------------------------
# JS Assets
# ------------------------------------------------------------------------------

class JSAsset(Asset):
    """Generator for JavaScript Assets."""

    def generate(self):
        get_spec = self.spec.get
        output = []; out = output.append
        for source in self.sources:
            print '   ', source
            if isinstance(source, Raw):
                out(source.text)
            elif source.endswith('.coffee'):
                out(do(['coffee', '-p', source]))
            else:
                out(read(source))
        output = ''.join(output)
        uglify = get_spec('uglify')
        if get_spec('compressed') or uglify:
            cmd = ['uglifyjs', '-nc', '--no-dead-code']
            if uglify:
                if isinstance(uglify, basestring):
                    cmd.append(uglify)
                else:
                    cmd.extend(uglify)
            popen = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE)
            output, stderr = popen.communicate(output)
            if stderr:
                exit("!! Got error uglifying: %s\n\n%s" % (self.path, stderr))
        self.emit(self.path, output)

register_handler('js', JSAsset)

# ------------------------------------------------------------------------------
# Asset Generator Runner
# ------------------------------------------------------------------------------

class AssetGenRunner(object):
    """Encapsulated asset generator runner."""

    manifest_changed = None
    manifest_path = None
    virgin = True

    def __init__(self, path, profile='default', force=None):
        self.manifest_changed = False
        data_dir = join(
            gettempdir(), 'assetgen-%s' % sha1(path).hexdigest()[:12]
            )

        if not isdir(data_dir):
            makedirs(data_dir)

        lock_path = join(data_dir, 'lock')
        lock(lock_path, path)

        self.config_path = path
        self.data_path = data_path = join(data_dir, 'data')
        self.force = force

        if isfile(data_path):
            data_file = open(data_path, 'rb')
            try:
                self.data = load(data_file)
            except Exception:
                self.data = {}
            data_file.close()
        else:
            self.data = {}

        config_file = open(path, 'rb')
        config_data = config_file.read() % os.environ
        self.config = config = decode_yaml(config_data)
        config_file.close()

        if not config:
            exit("No config found at %s" % path)

        if not isinstance(config, dict):
            exit("Config at %s is not a dict mapping." % path)

        for key in config.keys():
            if key.startswith('profile.'):
                if key == 'profile.%s' % profile:
                    profile_conf = config.pop(key)
                    config.update(profile_conf)
                else:
                    config.pop(key)

        for key in DEFAULTS:
            if key not in config:
                config[key] = DEFAULTS[key]

        if 'env' in config:
            env = config['env']
            for key, val in env.iteritems():
                if '.' in key:
                    key, action = key.split('.', 1)
                    existing = environ.get(key)
                    if action == 'prefix':
                        if existing:
                            environ[key] = "%s:%s" % (val, existing)
                            continue
                    elif action == 'append':
                        if existing:
                            environ[key] = "%s:%s" % (existing, val)
                            continue
                environ[key] = val

        self.base_dir = base_dir = dirname(path)
        output_dir = config.pop('output.directory', None)
        if not output_dir:
            exit("No value found for output.directory in %s." % path)

        self.output_dir = output_dir = join(base_dir, output_dir)
        self.output_template = config.pop('output.template')
        self.hashed = config.pop('output.hashed', False)

        manifest_path = config.pop('output.manifest', None)
        if manifest_path:
            self.manifest_path = join(base_dir, manifest_path)

        def expand_src(source):
            source = join(base_dir, source)
            if '*' not in source:
                return [source]
            root = split(source.partition('*')[0])[0]
            sources = []; new_source = sources.append
            for directory, _, files in walk(root):
                for file in files:
                    path = join(directory, file)
                    if fnmatch(path, source):
                        new_source(path)
            return sources

        for key in ('prereqs', 'generate'):

            listing = config.pop(key, None)
            if listing is None:
                exit("No value found for %s in %s." % (key, path))

            assets = []
            add_asset = assets.append
            setattr(self, key, assets)

            for info in listing:

                output, spec = info.items()[0]
                if 'type' in spec:
                    type = spec.pop('type')
                else:
                    if '.' not in output:
                        exit("Couldn't determine asset type for %r" % output)
                    type = output.rsplit('.', 1)[1]

                if type not in HANDLERS:
                    exit("No handler found for asset type %r." % type)

                prefix = '%s.' % type
                for conf_key in config:
                    if conf_key.startswith(prefix):
                        spec_key = conf_key.split('.', 1)[1]
                        if spec_key not in spec:
                            spec[spec_key] = config[conf_key]

                for key in spec.keys():
                    if key.startswith('profile.'):
                        if key == 'profile.%s' % profile:
                            profile_conf = spec.pop(key)
                            spec.update(profile_conf)
                        else:
                            spec.pop(key)

                _sources = spec.pop('source', None)
                if not _sources:
                    exit("No 'source' defined for %s" % output)

                if not isinstance(_sources, list):
                    _sources = [_sources]

                _depends = spec.pop('depends', [])
                if isinstance(_depends, basestring):
                    _depends = [_depends]

                depends = []
                for source in _depends:
                    depends.extend(expand_src(source))

                if output.endswith('/*'):
                    io = []; add_io = io.append
                    oprefix = output[:-1]
                    for source in _sources:
                        if isinstance(source, Raw):
                            exit("Source for %r cannot be raw text." % output)
                        if not source.endswith('/*'):
                            exit("Glob source %r must end in /* too." % source)
                        source = join(base_dir, source[:-1])
                        src_len = len(source)
                        for directory, _, files in walk(source):
                            for file in files:
                                path = join(directory, file)
                                _src = [path]
                                _dep = depends + _src
                                add_io((_src, _dep, oprefix + path[src_len:]))
                else:
                    sources = []
                    for source in _sources:
                        if isinstance(source, basestring):
                            sources.extend(expand_src(source))
                        else:
                            sources.append(Raw(source['raw']))
                    depends = depends + [
                        source for source in sources
                        if not isinstance(source, Raw)
                        ]
                    io = [(sources, depends, output)]

                for sources, depends, output in io:
                    if DEBUG:
                        print depends, '->', output
                    add_asset(
                        HANDLERS[type](self, output, sources, depends, spec)
                        )

    def clean(self):
        if 'prereq_data' in self.data:
            base_dir = self.base_dir
            prereq_data = self.data['prereq_data']
            for key, paths in prereq_data.iteritems():
                for path in paths:
                    full_path = join(base_dir, path)
                    print "=> Removing:", path
                    remove(full_path)
        output_dir = self.output_dir
        if isdir(output_dir):
            if output_dir.endswith("/"):
                print "=> Removing:", output_dir
            else:
                print "=> Removing:", output_dir + "/"
            rmtree(output_dir)
        data_path = self.data_path
        if isfile(data_path):
            print "=> Removing:", data_path
            remove(data_path)

    def emit(self, key, path, content, extension=''):
        directory, filename = split(path)
        if extension:
            root, ext = splitext(filename)
            filename = root + extension + ext
            path = join(directory, filename)
        if (not self.prereq) and self.hashed:
            digest = sha1(content).hexdigest()
            output_path = join(directory, self.output_template % {
                'hash': digest,
                'filename': filename
                })
        else:
            digest = None
            output_path = path
        if self.prereq:
            directory = join(self.base_dir, directory)
            real_output_path = join(self.base_dir, output_path)
        else:
            directory = join(self.output_dir, directory)
            real_output_path = join(self.output_dir, output_path)
        if not isdir(directory):
            makedirs(directory)
        file = open(real_output_path, 'wb')
        file.write(content)
        file.close()
        if self.prereq:
            self.prereq_data.setdefault(key, set()).add(path)
            print "=> Generated prereq:", output_path
            return
        self.output_data.setdefault(key, set()).add(output_path)
        if digest:
            print "=> Generated output: %s (%s)" % (path, digest[:6])
        else:
            print "=> Generated output:", path
        manifest = self.manifest
        self.manifest_changed = 1

        if path in manifest:
            ex_output_path = manifest[path]
            if output_path == ex_output_path:
                return
            ex_path = join(self.output_dir, ex_output_path)
            if isfile(ex_path):
                remove(ex_path)
                print ".. Removed stale:", ex_output_path
        manifest[path] = output_path

    def is_fresh(self, key, depends):
        if self.force:
            return
        mtime_cache = self.mtime_cache
        if self.prereq:
            output = join(self.base_dir, key)
            if not isfile(output):
                self.prereq_data.pop(key, None)
                return
            for dep in depends:
                if newer(dep, output, mtime_cache):
                    self.prereq_data.pop(key, None)
                    return
            if newer(self.config_path, output, mtime_cache):
                self.prereq_data.pop(key, None)
                return
            return 1
        paths = self.output_data.get(key)
        if not paths:
            return
        output_dir = self.output_dir
        for output in paths:
            output = join(output_dir, output)
            if not isfile(output):
                self.output_data.pop(key)
                return
        output = join(output_dir, list(paths).pop())
        for dep in depends:
            if newer(dep, output, mtime_cache):
                self.output_data.pop(key)
                return
        if newer(self.config_path, output, mtime_cache):
            self.output_data.pop(key)
            return
        return 1
        
    def run(self):
        chdir(self.base_dir)
        if self.virgin:
            if not isdir(self.output_dir):
                makedirs(self.output_dir)
            self.manifest = self.data.setdefault('manifest', {})
            self.output_data = self.data.setdefault('output_data', {})
            self.prereq_data = self.data.setdefault('prereq_data', {})
            self.virgin = False
        self.mtime_cache = {}
        self.prereq = True
        for asset in self.prereqs:
            if not asset.is_fresh():
                asset.generate()
        self.prereq = None
        for asset in self.generate:
            if not asset.is_fresh():
                asset.generate()
        manifest_path = self.manifest_path

        if manifest_path and self.manifest_changed:
            print "=> Updated manifest:", manifest_path
            manifest_file = open(manifest_path, 'wb')
            encode_json(self.manifest, manifest_file)
            manifest_file.close()
            self.manifest_changed = False
        data_file = open(self.data_path, 'wb')
        dump(self.data, data_file, 2)
        data_file.close()
        return

# ------------------------------------------------------------------------------
# Main Runner
# ------------------------------------------------------------------------------

def main(argv=None):

    argv = argv or sys.argv[1:]
    op = OptionParser(usage=(
        "Usage: assetgen [<path/to/assetgen.yaml> ...] [options]\n\n"
        "Note:\n"
        "    If you don't specify assetgen.yaml file paths, then `git\n"
        "    ls-files *assetgen.yaml` will be used to detect all config\n"
        "    files in the current repository. So you need to be inside\n"
        "    a git repository's working tree."
        ))

    op.add_option(
        '-v', '--version', action='store_true',
        help="show program's version number and exit"
        )

    op.add_option(
        '--clean', action='store_true', help="remove all generated files"
        )

    op.add_option(
        '--debug', action='store_true', help="set debug mode"
        )

    op.add_option(
        '--extension', action='append', dest='path',
        help="specify a python extension file (may be repeated)"
        )

    op.add_option(
        '--force', action='store_true', help="force rebuild of all files"
        )

    op.add_option(
        '--profile', dest='name', default='default',
        help="specify a profile to use"
        )

    op.add_option(
        '--watch', action='store_true',
        help="keep running assetgen on a loop"
        )

    autocomplete(op)
    options, files = op.parse_args(argv)

    if options.version:
        print 'assetgen 0.1'
        sys.exit()

    if options.debug:
        global DEBUG
        DEBUG = True

    clean = options.clean
    extensions = options.path
    force = options.force
    profile = options.name
    watch = options.watch

    if extensions:
        scope = globals()
        for ext in extensions:
            execfile(ext, scope, {})

    if files:
        for file in files:
            if not isfile(file):
                exit("Could not find %s" % file)

    if not files:
        if not is_git():
            op.print_help()
            sys.exit()
        root = SCMConfig().root
        files = run_command(
            ['git', 'ls-files', '*assetgen.yaml'], cwd=root
            ).strip().splitlines()
        if not files:
            op.print_help()
            sys.exit()
        files = [join(root, file) for file in files]

    generators = [
        AssetGenRunner(realpath(file), profile, force) for file in files
        ]

    if clean:
        for assetgen in generators:
            assetgen.clean()
        sys.exit()

    while 1:
        for assetgen in generators:
            assetgen.run()
        if watch:
            sleep(0.1)
        else:
            break

# ------------------------------------------------------------------------------
# Self Runner
# ------------------------------------------------------------------------------

if __name__ == '__main__':
    main()

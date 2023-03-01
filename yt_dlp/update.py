import atexit
import contextlib
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from zipimport import zipimporter

from .compat import functools  # isort: split
from .compat import compat_realpath, compat_shlex_quote
from .utils import (
    Popen,
    cached_method,
    deprecation_warning,
    remove_end,
    sanitized_Request,
    shell_quote,
    system_identifier,
    traverse_obj,
    version_tuple,
)
from .version import CHANNEL, UPDATE_HINT, VARIANT, __version__

UPDATE_SOURCES = {
    'stable': 'Grub4K/actions-test',
    'nightly': 'Grub4K/actions-archive-test',
}
WARN_BEFORE_TAG = (2023, 3, 2)
API_BASE_URL = 'https://api.github.com/repos'

# Backwards compatibility variables for the current channel
REPOSITORY = UPDATE_SOURCES[CHANNEL]
API_URL = f'{API_BASE_URL}/{REPOSITORY}/releases'


@functools.cache
def _get_variant_and_executable_path():
    """@returns (variant, executable_path)"""
    if getattr(sys, 'frozen', False):
        path = sys.executable
        if not hasattr(sys, '_MEIPASS'):
            return 'py2exe', path
        elif sys._MEIPASS == os.path.dirname(path):
            return f'{sys.platform}_dir', path
        elif sys.platform == 'darwin':
            machine = '_legacy' if version_tuple(platform.mac_ver()[0]) < (10, 15) else ''
        else:
            machine = f'_{platform.machine().lower()}'
            # Ref: https://en.wikipedia.org/wiki/Uname#Examples
            if machine[1:] in ('x86', 'x86_64', 'amd64', 'i386', 'i686'):
                machine = '_x86' if platform.architecture()[0][:2] == '32' else ''
        return f'{remove_end(sys.platform, "32")}{machine}_exe', path

    path = os.path.dirname(__file__)
    if isinstance(__loader__, zipimporter):
        return 'zip', os.path.join(path, '..')
    elif (os.path.basename(sys.argv[0]) in ('__main__.py', '-m')
          and os.path.exists(os.path.join(path, '../.git/HEAD'))):
        return 'source', path
    return 'unknown', path


def detect_variant():
    return VARIANT or _get_variant_and_executable_path()[0]


@functools.cache
def current_git_head():
    if detect_variant() != 'source':
        return
    with contextlib.suppress(Exception):
        stdout, _, _ = Popen.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            text=True, cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if re.fullmatch('[0-9a-f]+', stdout.strip()):
            return stdout.strip()


_FILE_SUFFIXES = {
    'zip': '',
    'py2exe': '_min.exe',
    'win_exe': '.exe',
    'win_x86_exe': '_x86.exe',
    'darwin_exe': '_macos',
    'darwin_legacy_exe': '_macos_legacy',
    'linux_exe': '_linux',
    'linux_aarch64_exe': '_linux_aarch64',
    'linux_armv7l_exe': '_linux_armv7l',
}

_NON_UPDATEABLE_REASONS = {
    **{variant: None for variant in _FILE_SUFFIXES},  # Updatable
    **{variant: f'Auto-update is not supported for unpackaged {name} executable; Re-download the latest release'
       for variant, name in {'win32_dir': 'Windows', 'darwin_dir': 'MacOS', 'linux_dir': 'Linux'}.items()},
    'source': 'You cannot update when running from source code; Use git to pull the latest changes',
    'unknown': 'You installed yt-dlp with a package manager or setup.py; Use that to update',
    'other': 'You are using an unofficial build of yt-dlp; Build the executable again',
}


def is_non_updateable():
    if UPDATE_HINT:
        return UPDATE_HINT
    return _NON_UPDATEABLE_REASONS.get(
        detect_variant(), _NON_UPDATEABLE_REASONS['unknown' if VARIANT else 'other'])


def _sha256_file(path):
    h = hashlib.sha256()
    mv = memoryview(bytearray(128 * 1024))
    with open(os.path.realpath(path), 'rb', buffering=0) as f:
        for n in iter(lambda: f.readinto(mv), 0):
            h.update(mv[:n])
    return h.hexdigest()


class Updater:
    def __init__(self, ydl, target=None):
        self.ydl = ydl
        self._exact = True

        if target is None:
            target = CHANNEL

        self._target_channel, sep, self._target_tag = target.rpartition('@')
        # Support for `--update-to stable`
        # It should become `stable@` and not `@stable`
        if (not sep) and self._target_tag in UPDATE_SOURCES:
            self._target_channel = self._target_tag
            self._target_tag = None

        if not self._target_channel:
            self._target_channel = CHANNEL

        if not self._target_tag:
            self._exact = False
            self._target_tag = 'latest'
        elif self._target_tag != 'latest':
            self._target_tag = f'tags/{self._target_tag}'

        if (WARN_BEFORE_TAG and re.fullmatch(r'(\d+\.?)*\d+', self._target_tag[5:])
                and version_tuple(self._target_tag) < WARN_BEFORE_TAG):
            self.ydl.report_warning('You are downgrading to a version without --update-to')

        self._target_repo = UPDATE_SOURCES.get(self._target_channel)

    def _version_compare(self, a, b):
        if CHANNEL != self._target_channel:
            return False

        a, b = version_tuple(a), version_tuple(b)
        return a == b if self._exact else a >= b

    @functools.cached_property
    def _tag(self):
        if self._version_compare(self.current_version, self.latest_version):
            return self._target_tag

        identifier = f'{detect_variant()} {self._target_channel} {system_identifier()}'
        for line in self._download('_update_spec', 'latest').decode().splitlines():
            if not line.startswith('lock '):
                continue
            _, tag, pattern = line.split(' ', 2)
            if re.match(pattern, identifier):
                if self._target_tag != 'latest':
                    try:
                        if version_tuple(tag) >= version_tuple(self._target_tag[5:]):
                            continue
                    except ValueError:
                        pass

                return f'tags/{tag}'
        return self._target_tag

    @cached_method
    def _get_version_info(self, tag):
        url = f'{API_BASE_URL}/{self._target_repo}/releases/{self._target_tag}'
        self.ydl.write_debug(f'Fetching release info: {url}')
        return json.loads(self.ydl.urlopen(sanitized_Request(url, headers={
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'yt-dlp',
            'X-GitHub-Api-Version': '2022-11-28',
        })).read().decode())

    @property
    def current_version(self):
        """Current version"""
        return __version__

    @property
    def _full_current_version(self):
        """Current version including channel"""
        return f'{CHANNEL}@{__version__}'

    @property
    def new_version(self):
        """Version of the latest release we can update to"""
        if self._tag.startswith('tags/'):
            return self._tag[5:]
        return self._get_version_info(self._tag)['tag_name']

    @property
    def _full_new_version(self):
        """Version of the latest release we can update to including channel"""
        return f'{self._target_channel}@{self.new_version}'

    @property
    def latest_version(self):
        """Version of the target release"""
        return self._get_version_info(self._target_tag)['tag_name']

    @property
    def has_update(self):
        """Whether there is an update available"""
        return not self._version_compare(self.current_version, self.new_version)

    @functools.cached_property
    def filename(self):
        """Filename of the executable"""
        return compat_realpath(_get_variant_and_executable_path()[1])

    def _download(self, name, tag):
        url = traverse_obj(self._get_version_info(tag), (
            'assets', lambda _, v: v['name'] == name, 'browser_download_url'), get_all=False)
        if not url:
            raise Exception('Unable to find download URL')
        self.ydl.write_debug(f'Downloading {name} from {url}')
        return self.ydl.urlopen(url).read()

    @functools.cached_property
    def release_name(self):
        """The release filename"""
        return f'yt-dlp{_FILE_SUFFIXES[detect_variant()]}'

    @functools.cached_property
    def release_hash(self):
        """Hash of the latest release"""
        hash_data = dict(ln.split()[::-1] for ln in self._download('SHA2-256SUMS', self._tag).decode().splitlines())
        return hash_data[self.release_name]

    def _report_error(self, msg, expected=False):
        self.ydl.report_error(msg, tb=False if expected else None)
        self.ydl._download_retcode = 100

    def _report_permission_error(self, file):
        self._report_error(f'Unable to write to {file}; Try running as administrator', True)

    def _report_network_error(self, action, delim=';'):
        target_tag = f'tag/{self._target_tag[5:]}' if self._target_tag.startswith('tags/') else self._target_tag
        self._report_error(
            f'Unable to {action}{delim} visit  '
            f'https://github.com/{self._target_repo}/releases/{target_tag}', True)

    def check_update(self):
        """Report whether there is an update available"""
        if not self._target_repo:
            self._report_error(
                f'No channel source for {self._target_channel!r} set. '
                f'Valid channels are {", ".join(UPDATE_SOURCES)}')
            return False

        try:
            self.ydl.to_screen(
                f'Available: {self._target_channel}@{self.latest_version}, Current: {self._full_current_version}')
        except Exception as err:
            self._report_network_error(f'obtain version info ({err})', delim='; Please try again later or')
            return False

        if self.has_update:
            if not is_non_updateable():
                self.ydl.to_screen(f'Current Build Hash: {_sha256_file(self.filename)}')
            return True

        if self._target_tag == self._tag:
            self.ydl.to_screen(f'yt-dlp is up to date ({self._full_current_version})')
        else:
            msg = 'to the specified version' if self._exact else 'any further'
            msg = f'yt-dlp cannot be updated {msg} since you are on an older Python version'
            if self._exact:
                self._report_error(msg, True)
            else:
                self.ydl.report_warning(msg)
        return False

    def update(self):
        """Update yt-dlp executable to the latest version"""
        if not self.check_update():
            return
        err = is_non_updateable()
        if err:
            return self._report_error(err, True)
        self.ydl.to_screen(f'Updating to {self._full_new_version} ...')

        directory = os.path.dirname(self.filename)
        if not os.access(self.filename, os.W_OK):
            return self._report_permission_error(self.filename)
        elif not os.access(directory, os.W_OK):
            return self._report_permission_error(directory)

        new_filename, old_filename = f'{self.filename}.new', f'{self.filename}.old'
        if detect_variant() == 'zip':  # Can be replaced in-place
            new_filename, old_filename = self.filename, None

        try:
            if os.path.exists(old_filename or ''):
                os.remove(old_filename)
        except OSError:
            return self._report_error('Unable to remove the old version')

        try:
            newcontent = self._download(self.release_name, self._tag)
        except OSError:
            return self._report_network_error('download latest version')
        except Exception as err:
            return self._report_network_error(f'fetch updates: {err}')

        try:
            expected_hash = self.release_hash
        except Exception:
            self.ydl.report_warning('no hash information found for the release')
        else:
            if hashlib.sha256(newcontent).hexdigest() != expected_hash:
                return self._report_network_error('verify the new executable')

        try:
            with open(new_filename, 'wb') as outf:
                outf.write(newcontent)
        except OSError:
            return self._report_permission_error(new_filename)

        if old_filename:
            mask = os.stat(self.filename).st_mode
            try:
                os.rename(self.filename, old_filename)
            except OSError:
                return self._report_error('Unable to move current version')

            try:
                os.rename(new_filename, self.filename)
            except OSError:
                self._report_error('Unable to overwrite current version')
                return os.rename(old_filename, self.filename)

        variant = detect_variant()
        if variant.startswith('win') or variant == 'py2exe':
            atexit.register(Popen, f'ping 127.0.0.1 -n 5 -w 1000 & del /F "{old_filename}"',
                            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif old_filename:
            try:
                os.remove(old_filename)
            except OSError:
                self._report_error('Unable to remove the old version')

            try:
                os.chmod(self.filename, mask)
            except OSError:
                return self._report_error(
                    f'Unable to set permissions. Run: sudo chmod a+rx {compat_shlex_quote(self.filename)}')

        self.ydl.to_screen(f'Updated yt-dlp to {self._full_new_version}')
        return True

    @functools.cached_property
    def cmd(self):
        """The command-line to run the executable, if known"""
        # There is no sys.orig_argv in py < 3.10. Also, it can be [] when frozen
        if getattr(sys, 'orig_argv', None):
            return sys.orig_argv
        elif getattr(sys, 'frozen', False):
            return sys.argv

    def restart(self):
        """Restart the executable"""
        assert self.cmd, 'Must be frozen or Py >= 3.10'
        self.ydl.write_debug(f'Restarting: {shell_quote(self.cmd)}')
        _, _, returncode = Popen.run(self.cmd)
        return returncode


def run_update(ydl):
    """Update the program file with the latest version from the repository
    @returns    Whether there was a successful update (No update = False)
    """
    return Updater(ydl).update()


# Deprecated
def update_self(to_screen, verbose, opener):
    import traceback

    deprecation_warning(f'"{__name__}.update_self" is deprecated and may be removed '
                        f'in a future version. Use "{__name__}.run_update(ydl)" instead')

    printfn = to_screen

    class FakeYDL():
        to_screen = printfn

        def report_warning(self, msg, *args, **kwargs):
            return printfn(f'WARNING: {msg}', *args, **kwargs)

        def report_error(self, msg, tb=None):
            printfn(f'ERROR: {msg}')
            if not verbose:
                return
            if tb is None:
                # Copied from YoutubeDL.trouble
                if sys.exc_info()[0]:
                    tb = ''
                    if hasattr(sys.exc_info()[1], 'exc_info') and sys.exc_info()[1].exc_info[0]:
                        tb += ''.join(traceback.format_exception(*sys.exc_info()[1].exc_info))
                    tb += traceback.format_exc()
                else:
                    tb_data = traceback.format_list(traceback.extract_stack())
                    tb = ''.join(tb_data)
            if tb:
                printfn(tb)

        def write_debug(self, msg, *args, **kwargs):
            printfn(f'[debug] {msg}', *args, **kwargs)

        def urlopen(self, url):
            return opener.open(url)

    return run_update(FakeYDL())


__all__ = ['Updater']

# Copyright (c) 2013 Yubico AB
# All rights reserved.
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import re
from wtforms.fields import IntegerField
from wtforms.validators import NumberRange, IPAddress, URL
from yubiadmin.util.app import App
from yubiadmin.util.config import (RegexHandler, FileConfig, php_inserter,
                                   parse_block, strip_comments, strip_quotes)
from yubiadmin.util.form import ConfigForm, FileForm, DBConfigForm, ListField
from yubiadmin.util.system import invoke_rc_d

__all__ = [
    'app'
]


def yk_pattern(varname, prefix='', suffix='', flags=None):
    regex = r'(?m)^(?!#)\$baseParams\[\'__YKVAL_%s__\'\]\s*=' \
            '\s*%s(.*?)%s\s*;\s*$' % (varname, prefix, suffix)
    if flags:
        regex = '(?%s)' % flags + regex
    return regex


def yk_write(varname, prefix='', suffix=''):
    return lambda x: '$baseParams[\'__YKVAL_%s__\'] = %s%s%s;' % \
        (varname, prefix, x, suffix)


def yk_handler(varname, default):
    return RegexHandler(yk_pattern(varname), yk_write(varname),
                        inserter=php_inserter, default=default)


def yk_parse_arraystring(value):
    return filter(None, [strip_quotes(x).strip() for x in strip_comments(value)
                         .split(',')])


def yk_array_handler(varname):
    pattern = yk_pattern(varname, 'array\(', '\)', 's')
    str_write = yk_write(varname, 'array(\n', '\n)')
    writer = lambda xs: str_write(',\n'.join(['\t"%s"' % x for x in xs]))
    reader = lambda match: yk_parse_arraystring(match.group(1))
    return RegexHandler(pattern, writer, reader, php_inserter, [])


QUOTED_STRS = re.compile(r'((?:"[^"]+")|(?:\'[^\']+\'))')


class KSMHandler(object):
    FUNCTION = re.compile(r'function\s+otp2ksmurls\s*\([^)]+\)\s*{')

    def _get_block(self, content):
        match = self.FUNCTION.search(content)
        if match:
            return parse_block(content[match.end():], '{', '}')
        return None

    def read(self, content):
        block = self._get_block(content)
        if block:
            quoted = QUOTED_STRS.findall(strip_comments(block))
            return [strip_quotes(x) for x in quoted]
        else:
            return []

    def write(self, content, value):
        block = self._get_block(content)
        value = ('function otp2ksmurls($otp, $client) {\n' +
                 '\treturn array (\n' +
                 '\m'.join(['\t\t"%s",' % x for x in value]) +
                 '\n\t);\n}')
        if block:
            match = self.FUNCTION.search(content)
            start = content[:match.start()]
            end = content[match.end() + len(block) + 1:]
            return start + value + end
        else:
            return php_inserter(content, value)


def is_daemon_running():
    return invoke_rc_d('ykval-queue', 'status')[0] == 0


ykval_config = FileConfig(
    '/etc/yubico/val/ykval-config.php',
    [
        ('sync_default', yk_handler('SYNC_DEFAULT_LEVEL', 60)),
        ('sync_secure', yk_handler('SYNC_SECURE_LEVEL', 40)),
        ('sync_fast', yk_handler('SYNC_FAST_LEVEL', 1)),
        ('default_timeout', yk_handler('SYNC_DEFAULT_TIMEOUT', 1)),
        ('sync_interval', yk_handler('SYNC_INTERVAL', 10)),
        ('resync_timeout', yk_handler('SYNC_RESYNC_TIMEOUT', 30)),
        ('old_limit', yk_handler('SYNC_OLD_LIMIT', 10)),
        ('sync_pool', yk_array_handler('SYNC_POOL')),
        ('allowed_sync_pool', yk_array_handler('ALLOWED_SYNC_POOL')),
        ('ksm_urls', KSMHandler())
    ]
)


class SyncLevelsForm(ConfigForm):
    legend = 'Sync Levels'
    description = 'Percentage of syncing required for pre-defined levels.'
    config = ykval_config

    sync_default = IntegerField('Default Level', [NumberRange(1, 100)])
    sync_secure = IntegerField('Secure Level', [NumberRange(1, 100)])
    sync_fast = IntegerField('Fast Level', [NumberRange(1, 100)])


class MiscForm(ConfigForm):
    legend = 'Misc'
    config = ykval_config

    default_timeout = IntegerField('Default Timeout (seconds)',
                                   [NumberRange(0)])


class DaemonForm(ConfigForm):
    legend = 'Daemon Settings'
    config = ykval_config
    sync_interval = IntegerField(
        'Sync Interval', [NumberRange(1)],
        description='How often (in seconds) to sync with other server.')
    resync_timeout = IntegerField('Resync Timeout', [NumberRange(1)])
    old_limit = IntegerField('Old Limit', [NumberRange(1)])


class SyncPoolForm(ConfigForm):
    legend = 'Sync Pool'
    config = ykval_config
    attrs = {
        'sync_pool': {'rows': 5, 'class': 'input-xxlarge'},
        'allowed_sync_pool': {'rows': 5, 'class': 'input-xxlarge'}
    }

    sync_pool = ListField(
        'Sync Pool URLs', [URL()],
        description="""
        List of URLs to other servers in the sync pool.<br />
        Example: <code>http://example.com/wsapi/2.0/sync</code>
        """)
    allowed_sync_pool = ListField(
        'Allowed Sync IPs', [IPAddress()],
        description="""
        List of IP-addresses of other servers that are allowed to sync with
        this server.<br />
        Example: <code>10.0.0.1</code>
        """)

    def save(self):
        super(SyncPoolForm, self).save()
        if is_daemon_running():
            invoke_rc_d('ykval-queue', 'restart')


class KSMForm(ConfigForm):
    legend = 'Key Store Modules'
    config = ykval_config
    attrs = {'ksm_urls': {'rows': 5, 'class': 'input-xxlarge'}}

    ksm_urls = ListField(
        'KSM URLs', [URL()],
        description="""
        List of URLs to KSMs.<br />
        The URLs must be fully qualified, i.e., contain the OTP itself.<br />
        Example: <code>http://example.com/wsapi/decrypt?otp=$otp</code><br />
        More advanced OTP to KSM mapping is possible by manually editing the
        configuration file.
        """)


class YubikeyVal(App):
    """
    YubiKey Validation Server

    YubiKey OTP validation server
    """

    name = 'val'
    sections = ['general', 'database', 'synchronization', 'ksms', 'advanced']

    def general(self, request):
        """
        General
        """
        return self.render_forms(request, [SyncLevelsForm(), MiscForm()])

    def database(self, request):
        """
        Database Settings
        """
        dbform = DBConfigForm('/etc/yubico/val/config-db.php',
                              dbname='ykval', dbuser='ykval_verifier')
        return self.render_forms(request, [dbform])

    def synchronization(self, request):
        """
        Synchronization
        """
        return self.render_forms(request, [DaemonForm(), SyncPoolForm()],
                                 template='val/synchronization',
                                 daemon_running=is_daemon_running())

    def daemon(self, request):
        if request.params['daemon'] == 'toggle':
            if is_daemon_running():
                invoke_rc_d('ykval-queue', 'stop')
            else:
                invoke_rc_d('ykval-queue', 'start')
        else:
            invoke_rc_d('ykval-queue', 'restart')

        return self.redirect('/%s/synchronization' % self.name)

    def ksms(self, request):
        """
        Key Store Modules
        """
        return self.render_forms(request, [KSMForm()])

    def advanced(self, request):
        """
        Advanced
        """
        return self.render_forms(request, [
            FileForm('/etc/yubico/val/ykval-config.php', 'Configuration')
        ])

    #Pulls the tab to the right:
    advanced.advanced = True

app = YubikeyVal()

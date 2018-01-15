#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import os
import sys
from datetime import datetime
import pylru
import collections
import yaml

from . import util
from .dbutil import DbManagedHost, DbManagedService
from .errors import AnsibleError, PkiNotSupported
from .ebsysconfig import SysConfig

__author__ = 'dusanklinec'
logger = logging.getLogger(__name__)

ANSIBLE_HOST_WRAPPER = '/etc/ansible/hosts.py'


class AnsiblePlaybooks(object):
    """
    Collection of default Ansible playbooks
    """
    def __init__(self):
        pass

    @staticmethod
    def nginx_reload():
        return {
            'name': 'Reload webserver',
            'service': 'name=nginx state=reloaded'
        }

    @staticmethod
    def apache_reload():
        return {
            'name': 'Reload webserver',
            'service': 'name=apache state=reloaded'
        }

    @staticmethod
    def privkey_fix(privkey_path=None):
        return {
            "name": "Privkey privileges",
            "file": {
                "path": "{{ privkey_path }}" if privkey_path is None else '%s' % privkey_path,
                "mode": 0o600
            }
        }

    @staticmethod
    def copy_certs(certs_src=None, certs_dst=None):
        return {
            "name": "Copy certificates",
            "copy": {
                "src": "{{ certs_src }}" if certs_src is None else '%s' % certs_src,
                "dest": "{{ certs_dst }}" if certs_dst is None else '%s' % certs_dst,
            }
        }

    @staticmethod
    def sync_certs(certs_src=None, certs_dst=None):
        return {
            "name": "Sync certs",
            "synchronize": {
                "src": "{{ certs_src }}/" if certs_src is None else '%s' % certs_src,
                "dest": "{{ certs_dst }}/" if certs_dst is None else '%s' % certs_dst,
                "checksum": "yes",
                "rsync_opts": [
                    "-L"
                ]
            }
        }

    @staticmethod
    def create_cert_dir(certs_dst=None):
        return {
            "name": "Cert directory",
            "file": "path=%s state=directory" % ('{{ certs_dst }}' if certs_dst is None else '%s' % {{ certs_dst }})
        }


class AnsibleWrapper(object):
    """
    Wrapper for the Ansible - executing tasks on hosts
    """
    def __init__(self, local_certbot_live=None, ansible_as_user=None, syscfg=None, **kwargs):
        """
        Initializes ansible wrapper
        :param local_certbot_live: local path to the certbot live directory
        :param ansible_as_user: sudo -u user for ansible
        :param syscfg:
        :type syscfg: SysConfig
        :param kwargs:
        """
        self.local_certbot_live = local_certbot_live
        self.ansible_as_user = ansible_as_user
        self.syscfg = syscfg  # type: SysConfig
        self.ansible_host_file = kwargs.get('ansible_host_file', '/etc/ansible/hosts.py')
        self.tmp_dir = kwargs.get('tmp_dir', '/tmp')

        # Last executed playbook
        self.last_playbook = None
        self.last_playbook_data = None

    @staticmethod
    def obj2yml(obj, **kwargs):
        """
        Transforms python object hierarchy to YML
        :param obj:
        :return:
        """
        kwargs.setdefault('default_flow_style', False)
        return yaml.dump(obj, **kwargs)

    def generate_cert_deploy_playbook(self, host, service, primary_domain):
        """
        Generates Ansible playbook for the certificate deployment

        :param host:
        :type host: DbManagedHost
        :param service:
        :type service: DbManagedService
        :param primary_domain:
        :return:
        :rtype: tuple[dict, dict]
        """
        if service is None or host is None:
            raise ValueError('Host or cert is None')

        if service.svc_ca is None or service.svc_ca.pki_type != 'LE':
            raise PkiNotSupported('PKI not supported')

        if not host.has_ansible:
            raise AnsibleError('Host is not Ansible configured')

        playbook = collections.OrderedDict()
        playbook_data = collections.OrderedDict()

        tasks = [
            AnsiblePlaybooks.create_cert_dir(),
            AnsiblePlaybooks.sync_certs(),
            AnsiblePlaybooks.privkey_fix(),
        ]

        if service.svc_provider == 'nginx':
            tasks.append(AnsiblePlaybooks.nginx_reload())
        elif service.svc_provider == 'apache':
            tasks.append(AnsiblePlaybooks.apache_reload())

        playbook['hosts'] = host.host_addr
        playbook['tasks'] = tasks

        # PKI dependent paths, svc dependent
        playbook_data['certs_src'] = os.path.join(self.local_certbot_live, primary_domain)
        playbook_data['certs_dst'] = os.path.join('/etc/letsencrypt/live', primary_domain)
        playbook_data['privkey_path'] = os.path.join(playbook_data['certs_dst'], 'privkey.pem')

        return playbook, playbook_data

    def run_ansible(self, cmds, ansible_cmd='ansible', cwd=None):
        """
        Runs ansible command
        :param cmds:
        :param ansible_cmd:
        :param cwd:
        :return:
        :rtype: tuple[int, string, string]
        """
        sudo_prefix = '' if self.ansible_as_user else 'sudo -E -H -u %s' % util.escape_shell(self.ansible_as_user)
        cmds = ' '.join(cmds) if isinstance(cmds, list) else cmds

        cmd = '%s %s %s -i %s' % (sudo_prefix, ansible_cmd, cmds, util.escape_shell(self.ansible_host_file))
        ret = self.syscfg.cli_cmd_sync(cmd=cmd, cwd=cwd, env={'ANSIBLE_STDOUT_CALLBACK': 'json'})
        return ret

    def deploy_certs(self, host, service, primary_domain):
        """
        Deploys certs to the host by running Ansible and returning the result.

        :param host:
        :type host: DbManagedHost
        :param service:
        :type service: DbManagedService
        :param primary_domain:
        :return:
        :rtype: tuple[int, string, string]
        """
        play_data = self.generate_cert_deploy_playbook(host=host, service=service, primary_domain=primary_domain)
        playbook, playbook_data = play_data
        self.last_playbook = playbook
        self.last_playbook_data = play_data

        # Generate playbook data to temp dir
        playbook_yml = AnsibleWrapper.obj2yml(playbook)
        fh_pl, fname_pl = util.unique_file(path=os.path.join(self.tmp_dir, 'cert_playbook.yml'), mode=0o600)
        fh_pld, fname_pld = util.unique_file(path=os.path.join(self.tmp_dir, 'cert_playbook_data.json'), mode=0o600)
        with fh_pl, fh_pld:
            fh_pl.write(playbook_yml)
            fh_pld.write(json.dumps(playbook_data, indent=2, cls=util.AutoJSONEncoder))

        cmds = '-l %s --extra-vars "@%s" %s ' % (
            host.host_addr,
            util.escape_shell(fname_pld),
            util.escape_shell(fname_pl)
        )
        ret = self.run_ansible(cmds=cmds, ansible_cmd='ansible-playbook', cwd=self.tmp_dir)
        out = util.try_load_json(ret[1])

        return ret[0], out, ret[2]

    def get_facts(self, host_id=None):
        """
        Gets all facts from the hosts
        ansible host_id -m setup

        :param host_id:
        :return:
        """
        if host_id is None:
            host_id = ''

        cmds = '%s -m setup' % host_id
        ret = self.run_ansible(cmds=cmds, ansible_cmd='ansible', cwd=self.tmp_dir)
        out = util.try_load_json(ret[1])
        return ret[0], out, ret[2]

    def ping(self, host_id):
        """
        Pings the host
        ansible host_id -m ping

        :param host_id:
        :return:
        """
        if host_id is None:
            host_id = ''

        cmds = '%s -m ping' % host_id
        ret = self.run_ansible(cmds=cmds, ansible_cmd='ansible', cwd=self.tmp_dir)
        out = util.try_load_json(ret[1])
        return ret[0], out, ret[2]




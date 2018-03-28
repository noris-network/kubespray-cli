#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# This file is part of Kubespray.
#
#    Kubespray is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Foobar is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Foobar.  If not, see <http://www.gnu.org/licenses/>.

"""
kubespray.cloud
~~~~~~~~~~~~

Run Instances on cloud providers and generate inventory
"""
import json
import os
import shutil
import sys
import yaml

from kubespray.inventory import CfgInventory
from kubespray.common import (get_logger, query_yes_no, run_command, which,
                              id_generator, get_cluster_name)
from ansible.utils.display import Display


noalias_dumper = yaml.dumper.SafeDumper
noalias_dumper.ignore_aliases = lambda self, data: True

display = Display()
playbook_exec = which('ansible-playbook')


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]


def distribute_hosts(hosts_zones):
    """
    Given  [(['host1', 'host2', 'host3'], 'A'), (['host4', 'host5'], 'B')]
    return:
    [{name: host1, zone: A},
     {name: host2, zone: A},
     {name: host3, zone: A},
     {name: host4, zone: B},
     {name: host5, zone: B}]
    """
    for item in hosts_zones:
        hosts, zone = item[0], item[1]
        for host in hosts:
            yield {"name": host, "zone": zone}


try:
    import configparser
except ImportError:
    import configparser as configparser


class Cloud(object):
    '''
    Run Instances on cloud providers and generates inventory
    '''

    def __init__(self, options, cloud):
        self.options = options
        self.cloud = cloud
        self.inventorycfg = options['inventory_path']
        self.playbook = os.path.join(options['kubespray_path'], 'local.yml')
        self.cparser = configparser.ConfigParser(allow_no_value=True)
        self.Cfg = CfgInventory(options, cloud)
        self.localcfg = os.path.join(
            options['kubespray_path'], 'inventory/local.cfg'
        )
        self.instances = {
            'masters': {
                'file': os.path.join(
                    options['kubespray_path'], 'masters_instances.json'
                ),
                'json': None,
            },
            'nodes': {
                'file': os.path.join(
                    options['kubespray_path'], 'nodes_instances.json'
                ),
                'json': None,
            },
            'etcds': {
                'file': os.path.join(
                    options['kubespray_path'], 'etcds_instances.json'
                ),
                'json': None,
            },
        }
        self.logger = get_logger(
            options.get('logfile'), options.get('loglevel')
        )
        self.pbook_content = [
            {
                'gather_facts': False,
                'hosts': 'localhost',
                'become': False,
                'tasks': [],
            }
        ]
        self.logger.debug(
            '''
             The following options were used to generate the inventory: %s
             ''' %
            self.options
        )

    def write_local_inventory(self):
        '''Generates inventory for local tasks'''
        self.cparser.add_section('local')
        self.cparser.set(
            'local',
            'localhost ansible_connection=local',
        )
        try:
            with open(self.localcfg, 'w') as f:
                self.cparser.write(f)
        except IOError as e:
            display.error('Cannot write inventory %s: %s' % (self.localcfg, e))
            sys.exit(1)

    def write_playbook(self):
        '''Write the playbook for instances creation'''
        try:
            with open(self.playbook, "w") as pb:
                pb.write(
                    yaml.dump(self.pbook_content, default_flow_style=False,
                              Dumper=noalias_dumper)
                )
        except IOError as e:
            display.error(
                'Cant write the playbook %s: %s' % (self.playbook, e)
            )
            sys.exit(1)

    def write_inventory(self):
        '''Generate the inventory according the instances created'''
        for role in ['masters', 'nodes', 'etcds']:
            if '%s_count' % role in list(self.options.keys()):
                with open(self.instances['%s' % role]['file']) as f:
                    self.instances['%s' % role]['json'] = json.load(f)
            else:
                self.instances['%s' % role]['json'] = []

        self.Cfg.write_inventory(
            self.instances['masters']['json'],
            self.instances['nodes']['json'],
            self.instances['etcds']['json'],
        )

    def create_instances(self):
        '''Run ansible-playbook for instances creation'''
        cmd = [
            playbook_exec,
            '-i',
            self.localcfg,
            '-e',
            'ansible_connection=local',
            self.playbook,
        ]
        if self.options['ansible_opts']:
            cmd = cmd + self.options['ansible_opts']
        if not self.options['assume_yes']:
            count = 0
            for role in ['masters', 'nodes', 'etcds']:
                if '%s_count' % role in list(self.options.keys()):
                    count = count + self.options['%s_count' % role]
            if self.options['add_node']:
                display.warning(
                    '%s node(s) will be added to the current inventory %s' %
                    (count, self.inventorycfg)
                )
            if not query_yes_no(
                'Create %s instances on %s ?' % (count, self.cloud)
            ):
                display.display('Aborted', color='red')
                sys.exit(1)

        display.display(" ".join(cmd))
        rcode, emsg = run_command('Create %s instances' % self.cloud, cmd)
        if rcode != 0:
            self.logger.critical('Cannot create instances: %s' % emsg)
            sys.exit(1)

    def update_group_vars(self):
        """
        Based on kubespray.yml we modify group_vars/all.yml
        and group_vars/k8s-cluster.
        """
        custom_group_vars = self.options.get("custom_group_vars")
        if not custom_group_vars:
            return

        group_vars_path = os.path.join(self.options['kubespray_path'],
                                       "inventory", "group_vars")
        for name, values in self.options["custom_group_vars"].items():
            config_path = os.path.join(group_vars_path, name) + '.yml'
            shutil.copy(config_path, config_path + ".orig")

            config = yaml.load(open(config_path, 'r'))
            config.update(values)

            with open(config_path, 'w') as new_config:
                new_config.write(
                    yaml.dump(config, default_flow_style=False,
                              Dumper=noalias_dumper))

            display.display(
                'Group vars updated: %s' % config_path, color='green')


class AWS(Cloud):

    def __init__(self, options):
        Cloud.__init__(self, options, "aws")
        self.options = options

    def gen_cloud_playbook(self):
        self.gen_ec2_playbook()

    def gen_ec2_playbook(self):
        # Options list of ansible EC2 module
        self.options['image'] = self.options['ami']
        if 'security_group_id' in list(self.options.keys()):
            self.options['group_id'] = self.options['security_group_id']
        if 'security_group_name' in list(self.options.keys()):
            self.options['group'] = self.options['security_group_name']
        if 'tags' in self.options:
            self.options['instance_tags'] = {}
            for kv in self.options['tags']:
                k, v = kv.split("=")
                self.options['instance_tags'][k] = v
        ec2_options = [
            'aws_access_key',
            'aws_secret_key',
            'count',
            'group_id',
            'group',
            'instance_type',
            'instance_profile_name',
            'key_name',
            'vpc_subnet_id',
            'image',
            'instance_tags',
            'assign_public_ip',
            'region',
        ]
        # Define EC2 task
        for role in ['masters', 'nodes', 'etcds']:
            if '%s_count' % role in list(self.options.keys()):
                ec2_task = {
                    'ec2': {},
                    'name': 'Provision EC2 %s instances' % role,
                    'register': 'ec2_%s' % role,
                }
                for opt in ec2_options:
                    if opt in list(self.options.keys()):
                        d = {opt: self.options[opt]}
                        ec2_task['ec2'].update(d)
                ec2_task['ec2'].update(
                    {'count': self.options['%s_count' % role]}
                )
                ec2_task['ec2'].update(
                    {'instance_type': self.options['%s_instance_type' % role]}
                )
                ec2_task['ec2'].update(
                    {
                        'instance_profile_name': self.options[
                            '%s_instance_profile_name' % role
                        ]
                    }
                )
                ec2_task['ec2'].update({'wait': True})
                self.pbook_content[0]['tasks'].append(ec2_task)
                # Write ec2 instances json
                self.pbook_content[0]['tasks'].append(
                    {
                        'name': 'Generate a file with ec2 instances list',
                        'copy': {
                            'dest': '%s' % self.instances['%s' % role]['file'],
                            'content': '{{ec2_%s.instances}}' % role,
                        },
                    }
                )
                # Wait for ssh task
                if self.options['use_private_ip']:
                    instance_ip = '{{ item.private_ip }}'
                else:
                    instance_ip = '{{ item.public_ip }}'
                self.pbook_content[0]['tasks'].append(
                    {
                        'local_action': {
                            'host': '%s' % instance_ip,
                            'module': 'wait_for',
                            'port': 22,
                            'state': 'started',
                            'timeout': 600,
                        },
                        'name': 'Wait until SSH is available',
                        'with_items': '{{ec2_%s.instances}}' % role,
                    }
                )

        self.write_local_inventory()
        self.write_playbook()


class GCE(Cloud):

    def __init__(self, options):
        Cloud.__init__(self, options, "gce")
        self.options = options

    def gen_cloud_playbook(self):
        self.gen_gce_playbook()

    def gen_gce_playbook(self):

        if 'tags' in self.options:
            self.options['tags'] = ','.join(self.options['tags'])
        # Options list of ansible GCE module
        gce_options = [
            'machine_type',
            'image',
            'zone',
            'service_account_email',
            'pem_file',
            'credentials_file',
            'project_id',
            'tags',
            'network',
            'subnetwork',
        ]
        # Define instance names
        cluster_name = 'k8s-' + get_cluster_name()
        for role in ['masters', 'nodes', 'etcds']:
            gce_instance_names = list()
            if '%s_count' % role in list(self.options.keys()):
                for x in range(self.options['%s_count' % role]):
                    if self.options['add_node']:
                        current_inventory = self.Cfg.read_inventory()
                        cluster_name = '-'.join(
                            current_inventory['all']['hosts'][0][
                                'hostname'
                            ].split(
                                '-'
                            )[
                                :-2
                            ]
                        )
                        gce_instance_names.append(
                            cluster_name + '-%s' % id_generator()
                        )
                    elif 'cluster_name' in list(self.options.keys()):
                        gce_instance_names.append(
                            self.options['cluster_name'] +
                            '-%s' %
                            id_generator()
                        )
                    else:
                        gce_instance_names.append(
                            cluster_name + '-%s' % id_generator()
                        )
                gce_instance_names = ','.join(gce_instance_names)
                # Define GCE task
                gce_task = {
                    'gce': {},
                    'name': 'Provision GCE %s instances' % role,
                    'register': 'gce_%s' % role,
                }
                for opt in gce_options:
                    if opt in list(self.options.keys()):
                        d = {opt: self.options[opt]}
                        gce_task['gce'].update(d)
                gce_task['gce'].update(
                    {'machine_type': self.options['%s_machine_type' % role]}
                )
                gce_task['gce'].update(
                    {'instance_names': '%s' % gce_instance_names}
                )
                self.pbook_content[0]['tasks'].append(gce_task)
                # Write gce instances json
                self.pbook_content[0]['tasks'].append(
                    {
                        'name': 'Generate a file with %s list' % role,
                        'copy': {
                            'dest': '%s' % self.instances['%s' % role]['file'],
                            'content': '{{gce_%s.instance_data}}' % role,
                        },
                    }
                )
                # Wait for ssh task
                if self.options['use_private_ip']:
                    instance_ip = '{{ item.private_ip }}'
                else:
                    instance_ip = '{{ item.public_ip }}'

                self.pbook_content[0]['tasks'].append(
                    {
                        'local_action': {
                            'host': '%s' % instance_ip,
                            'module': 'wait_for',
                            'port': 22,
                            'state': 'started',
                            'timeout': 600,
                        },
                        'name': 'Wait until SSH is available',
                        'with_items': '{{gce_%s.instance_data}}' % role,
                    }
                )
        self.write_local_inventory()
        self.write_playbook()


class OpenStack(Cloud):

    def __init__(self, options):
        Cloud.__init__(self, options, 'openstack')
        self.options = options

    def gen_cloud_playbook(self):
        self.gen_openstack_playbook()

    def gen_openstack_playbook(self):

        openstack_credential_args = (
            'auth_url', 'username', 'password', 'project_name'
        )
        openstack_auth = {}

        for cred_arg in openstack_credential_args:
            openstack_auth.update(
                {
                    cred_arg: os.environ.get(
                        "OS_%s" % cred_arg.upper(),
                        self.options.get('os_%s' % cred_arg),
                    )
                }
            )

        _diff = set(openstack_credential_args) - set(openstack_auth.keys())

        if _diff:
            print("%s not found in the configuration or environment" % _diff)
            sys.exit(2)

        if 'os_domain_name' in self.options:
            openstack_auth.update(
                {'domain_name': self.options['os_domain_name']}
            )

        if self.options['floating_ip']:
            ip_type = 'public'
        else:
            ip_type = 'private'

        # Define instance names
        cluster_name = 'k8s-' + self.options['cluster_name']
        os_security_group_name = cluster_name + '-%s' % id_generator()

        self.pbook_content[0]['tasks'].append(
            {
             'name': 'Create security group',
             'os_security_group': {
                 'auth': openstack_auth,
                 'name': os_security_group_name,
                 'description': ('Contains security rules for the Kubernetes'
                                 ' cluster'),
                 'region_name': os.environ.get(
                        "OS_REGION_NAME", self.options['os_region_name']
                        ),
                 'state': 'present',
                },
            }
        )
        self.pbook_content[0]['tasks'].append(
            {
                'name': 'Add security rules',
                'os_security_group_rule': {
                    'auth': openstack_auth,
                    'security_group': os_security_group_name,
                    'protocol': '{{item}}',
                    'region_name': os.environ.get(
                        "OS_REGION_NAME", self.options['os_region_name']
                    ),
                    'state': 'present',
                },
                'with_items': ['tcp', 'udp', 'icmp'],
            }
        )

        for role in ('masters', 'nodes', 'etcds'):
            os_instance_names = list()
            if '%s_count' % role in list(self.options.keys()):
                for x in range(self.options['%s_count' % role]):
                    if self.options['add_node']:
                        current_inventory = self.Cfg.read_inventory()
                        cluster_name = '-'.join(
                            current_inventory['all']['hosts'][0][
                                'hostname'
                            ].split(
                                '-'
                            )[
                                :-1
                            ]
                        )
                        os_instance_names.append(
                            cluster_name + '-%s' % id_generator()
                        )
                    else:
                        os_instance_names.append(
                            cluster_name + '-%s' % id_generator()
                        )
                self.pbook_content[0]['tasks'].append(
                    {
                        'name': 'Create %s network ports' % role,
                        'os_port': {
                            'auth': openstack_auth,
                            'name': '{{item}}',
                            'region_name': os.environ.get(
                                "OS_REGION_NAME",
                                self.options['os_region_name'],
                            ),
                            'network': self.options['network'],
                            'allowed_address_pairs': [
                                {'ip_address': self.options['kube_network']}
                            ],
                            'security_groups': [os_security_group_name],
                            'state': 'present',
                        },
                        'with_items': os_instance_names,
                    }
                )

                host_zones = None
                if self.options.get("os_availability_zones"):
                    # brain fuck warning
                    # this divides the lists of hosts into zones
                    # >>> hosts
                    # >>> ['host1', 'host2', 'host3', 'host4', 'host5']

                    # >>> zones
                    # >>> ['A', 'B']

                    # >>> list(zip([hosts[i:i + n] for i in range(0, len(hosts), n)], zones)) # noqa
                    # >>> [(['host1', 'host2', 'host3'], 'A'), (['host4', 'host5'], 'B')]  # noqa
                    hosts, zones = os_instance_names, self.options[
                        'os_availability_zones']

                    if len(zones) == len(hosts):
                        host_zones = [{"name": k, "zone": v} for
                                      k, v in zip(hosts, zones)]
                    else:
                        end = len(zones) + 1 if len(zones) % 2 else len(zones)
                        host_zones = list(zip([hosts[i:i + end] for i in
                                               range(0, len(hosts), end)],
                                              zones))
                        host_zones = list(distribute_hosts(host_zones))

                provision_item = {
                    "name": 'Provision OS %s instances' % role,
                    "register": 'os_%s' % role,
                    }

                net_id = self.options.get('os_network_id')
                net_id = ",net-id=%s" % net_id if net_id else ""
                os_server_item = {
                            'auth': openstack_auth,
                            'name': '{{item.name}}',
                            'state': 'present',
                            'flavor': self.options['%s_flavor' % role],
                            'key_name': self.options['sshkey'],
                            'region_name': self.options['os_region_name'],
                            'auto_ip': self.options['floating_ip'],
                            'security_groups': [os_security_group_name],
                            'nics': 'port-name={{ item.name }}' + net_id,
                            'image': self.options['image'],
                            'boot_from_volume': self.options.get(
                                '%s_boot_from_volume' % role, True
                            ),
                            'volume_size': self.options[
                                '%s_volume_size' % role
                            ],
                            'userdata': self.options.get("userdata", "")
                            }

                if host_zones:
                    os_server_item["availability_zone"] = "{{item.zone}}"

                    provision_item["os_server"] = os_server_item
                    provision_item["loop"] = host_zones
                else:
                    provision_item["loop"] = [
                        {"name": name} for name in os_instance_names]

                self.pbook_content[0]['tasks'].append(provision_item)

                # Write os instances json
                self.pbook_content[0]['tasks'].append(
                    {
                        'name': 'Generate a file with OS %s instances list' %
                        role,
                        'copy': {
                            'dest': '%s' % self.instances[role]['file'],
                            'content': '{{os_%s.results}}' % role,
                        },
                    }
                )
                # Wait for ssh task
                self.pbook_content[0]['tasks'].append(
                    {
                        'name': 'Wait until SSH is available',
                        'wait_for': {
                            'host': '{{item.openstack.%s_v4}}' % ip_type,
                            'port': 22,
                            'search_regex': 'SSH',
                            'state': 'started',
                            'delay': 10,
                        },
                        'with_items': '{{os_%s.results}}' % role,
                    }
                )
        self.write_local_inventory()
        self.write_playbook()

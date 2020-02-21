"""
Network contains almost all the networking tools for AutoDist.

The notable exception is ResourceSpec.

Network will be used by other modules to handle:
1) Copying files
2) Writing files
3) Running code
on nodes in the cluster defined by the ResourceSpec. 

Prerequisites:
* TensorFlow is already installed in the env of all nodes.
* Only supports graph launching logic. Only one node (the Chief) runs the session client.
* AutoDist is already installed in the env of the worker node Chief, where the main script runs.
* The SSH private key to other nodes is accessible by AutoDist on Chief given a path.
"""
import atexit
import contextlib
import json
import os
import signal
import subprocess
import sys
import warnings
from abc import ABCMeta, abstractmethod
from ipaddress import ip_address
from typing import Dict, NamedTuple, Optional

import netifaces
import paramiko

from autodist.const import DEFAULT_PORT_RANGE, DEFAULT_WORKING_DIR, ENV
from autodist.utils import logging

warnings.filterwarnings(action='ignore', module=paramiko.__name__)


class SSHConfig(NamedTuple):
    """Contains any necessary SSH information (e.g. passwords, keyfiles, etc.)."""

    username: str
    port: int
    python_venv: str
    key_file: str
    pkey: Optional[paramiko.RSAKey]
    env: dict


class SSHConfigMap(dict):
    """Contains all necessary SSH configs, grouped by config name."""

    def __init__(self, info: Dict[str, Dict], node_groups: Dict[str, str]):
        """
        Initialize the object with a dictionary of SSH information.

        Args:
            info (dict): any SSH information needed for remote control.
                This dict should map from identifier to dict of SSH info
                (username, port, keyfile, etc.).
            node_groups (dict): mapping from hostnames to SSH group names.
        """
        super().__init__()

        # Construct SSH Group to SSH Config mapping
        conf_map = {}
        for key, ssh_info in info.items():
            # Parse out information from sub-dict
            conf_map[key] = SSHConfig(
                username=ssh_info.get('username', ''),
                port=ssh_info.get('port', 22),
                python_venv=ssh_info.get('python_venv', ''),
                key_file=ssh_info.get('key_file', ''),
                pkey=self._gen_rsa_pkey(ssh_info.get('key_file', None)),
                env=dict(
                    TF_CPP_MIN_LOG_LEVEL=0,
                    AUTODIST_PATCH_TF=ENV.AUTODIST_PATCH_TF.val,
                    **ssh_info.get('shared_envs', {})
                )
            )

        # Use conf_map to construct Hostname to SSH Config mapping
        for hostname, group in node_groups.items():
            self[hostname] = conf_map.get(group)

    @staticmethod
    def _gen_rsa_pkey(key_file_path: str):
        if not key_file_path:
            return None
        return paramiko.RSAKey.from_private_key_file(os.path.expanduser(os.path.abspath(key_file_path)))


class Cluster(metaclass=ABCMeta):
    """Cluster manager for TensorFlow servers."""

    def __init__(self, resource_spec):
        self.cluster_spec = self._get_default_cluster_spec(resource_spec)
        self._chief = resource_spec.chief
        self._full_addresses = [full_address for tasks in self.cluster_spec.values() for full_address in tasks]
        # noinspection PyTypeChecker
        self._address_to_port = dict(a.split(':') for a in self._full_addresses)
        self._task_to_address = {
            (job_name, task_index): a.split(':')[0]
            for job_name, tasks in self.cluster_spec.items()
            for task_index, a in enumerate(tasks)
        }
        self.subprocesses = []
        logging.info('ClusterSpec: {}'.format(self.cluster_spec))

    @staticmethod
    def _get_default_cluster_spec(resource_spec):
        """Create list of workers from the resource spec with semi-arbitrarily chosen ports."""
        return {
            'worker': [
                '{ip}:{port}'.format(
                    ip=n,
                    port=next(DEFAULT_PORT_RANGE)
                    # sorted is important.
                    # we need to guarantee the ip-port mapping to be the same in every worker.
                ) for n in sorted(resource_spec.nodes)
            ]
        }

    def is_chief(self, address=None):
        """
        Check whether an address is chief or not.

        If the argument `address` is not provided,
        it will check whether the local address is chief.

        Args:
            address (str): node address e.g. ip

        Returns:
            bool:
        """
        address = address or self.get_local_address()
        return address == self._chief

    def get_address_from_task(self, job_name, task_index):
        """
        Given a job name and task index, return the address.

        Args:
            job_name (str): job name
            task_index (int): task index

        Returns:
            str
        """
        return self._task_to_address[(job_name, task_index)]

    def get_local_address(self):
        """
        Get the local (ip) address.

        Returns:
            str: worker ip or chief address by default.
        """
        return ENV.AUTODIST_WORKER.val or self._chief

    def get_local_worker_task_index(self):
        """
        Get the (first) TensorFlow task index of the "worker" for the local.

        Returns:
            int: task index
        """
        return [i for i, a in enumerate(self._full_addresses) if self.get_local_address() in a][0]

    def get_local_session_target(self):
        """
        Get the session target of the local session.

        Returns:
            str:
        """
        port = self._address_to_port[self.get_local_address()]
        return 'grpc://localhost:' + port

    def start(self):
        """
        Start tf.servers on all nodes.

        Note that this only runs (and only should run) on the chief node.
        """
        # pylint: disable=import-outside-toplevel
        from autodist.utils import server_starter

        # atexit registration should be placed
        #   - before the beginning of the start
        #   (to ensure the clean termination if the start fails in its half way); and
        #   - at the same module as the start
        #   (to follow the python assumption that
        #   lower level modules will normally be imported
        #   before higher level modules and thus must be cleaned up later).
        atexit.register(self.terminate)
        envs = {ENV.AUTODIST_MIN_LOG_LEVEL.name: 'ERROR'}
        envs = ['{}={}'.format(k, v) for k, v in envs.items()]
        module_name = server_starter.__name__
        module_file = server_starter.__file__

        if self.is_chief():
            self._clean_autodist_processes(module_name)

        for job_name, tasks in self.cluster_spec.items():
            for task_index, full_address in enumerate(tasks):
                address = full_address.split(':')[0]
                args = ['--job_name=%s' % job_name, '--task_index=%d' % task_index]

                if is_local_address(address) or self.is_chief(address):
                    json.dump(self.cluster_spec, open(os.path.join(DEFAULT_WORKING_DIR, 'cluster_spec.json'), 'w+'))

                    cmd = envs + [sys.executable, '-m', module_name] + args

                    # pylint: disable=subprocess-popen-preexec-fn
                    proc = subprocess.Popen(' '.join(cmd), shell=True, preexec_fn=os.setsid)
                    self.subprocesses.append(proc)
                    # The above line immediately follows the Popen
                    # to ensure no gap for termination failure due to the empty proc list.
                    logging.debug('$ local tf.server started at {}: job_name={} task_index={}'.format(
                        full_address, job_name, task_index
                    ))
                else:  # remote
                    self.remote_pre_start_tf_server(address, tf_server_starter_filepath=module_file)
                    file = os.path.join(DEFAULT_WORKING_DIR, os.path.basename(module_file))
                    bash = envs + ['python', '-u', file] + args
                    logging.info("Launching tf.server on %s" % address)
                    proc = self.remote_exec(bash, hostname=address)
                    # The above line immediately follows the Popen
                    # to ensure no gap for termination failure due to the empty proc list.
                    self.subprocesses.append(proc)

    def terminate(self):
        """Terminate."""
        logging.info('Terminating cluster...')
        for p in self.subprocesses:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)

    def remote_pre_start_tf_server(self, hostname, tf_server_starter_filepath, working_dir=DEFAULT_WORKING_DIR):
        """
        Prepare to start a TensorFlow server remotely.

        Args:
            hostname (str): host name or address
            tf_server_starter_filepath (str): local starter file path
            working_dir (str): remote working directory
        """
        logging.info("Copying necessary files to %s" % hostname)
        self.remote_copy(local_path=tf_server_starter_filepath, remote_path=working_dir, hostname=hostname)
        self.remote_file_write(
            remote_path=os.path.join(working_dir, 'cluster_spec.json'),
            data=json.dumps(self.cluster_spec),
            hostname=hostname,
        )

    def _clean_autodist_processes(self, process_keyword):
        """
        Kills any autodist processes on all non-chief nodes.

        Args:
            process_keyword (str): A keyword of the autodist process (usually the server starter file).
        """
        cmd = "ps aux | awk '\\''! /awk/ && /{}/ {{print \\$2}}'\\'' | xargs kill -9".format(process_keyword)
        cmd_list = cmd.split(" ")
        procs = []
        for hostname in self._address_to_port.keys():
            logging.info("Cleaning autodist processes on %s" % hostname)
            if self.is_chief(hostname):
                # The above `cmd` is escaped for a string-within-a-string, so we have to nest `bash -c`
                # There's probably a better way to do this
                local_cmd = 'bash -c \'bash -c "{}"\''.format(cmd)
                proc = subprocess.Popen(local_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            else:
                proc = self.remote_exec(cmd_list, hostname)
            procs.append(proc)

        # We don't want this to interact with server-starting later
        for proc in procs:
            proc.wait()

    @abstractmethod
    def remote_exec(self, args, hostname):
        """
        Execute a bash script remotely.

        Args:
            args (list): bash commands
            hostname (str): host name or address

        Returns:
            Process: process handle
        """

    @abstractmethod
    def remote_file_write(self, remote_path, data, hostname):
        """
        Write a remote file.

        Args:
            remote_path (str): remote file path
            data (str): data to be written
            hostname (str): host name or address
        """

    @abstractmethod
    def remote_copy(self, local_path, remote_path, hostname):
        """
        Copy a file to a remote directory.

        Args:
            local_path (str): local file path to be copied
            remote_path (str): remote directory path
            hostname (str): host name or address
        """


class SSHCluster(Cluster):
    """An AutoDist Cluster Based on SSH."""

    def __init__(self, resource_spec):
        self._ssh_conf = resource_spec.ssh_config_map
        super().__init__(resource_spec)

    @contextlib.contextmanager
    def _get_ssh_client(self, hostname):
        """
        Get a Paramiko SSH Client to the given node.

        Args:
            hostname (str): The node to SSH into.

        Returns:
            Yields a Paramiko SSHClient.
        """
        ssh_config = self._ssh_conf[hostname]
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.WarningPolicy)
        client.connect(hostname=hostname, port=ssh_config.port, username=ssh_config.username, pkey=ssh_config.pkey)
        yield client
        client.close()

    @contextlib.contextmanager
    def _get_sftp_client(self, hostname):
        """
        Get a Paramiko SFTP Client to the given node.

        Args:
            hostname (str): The node to SFTP to.

        Returns:
            Yields a Paramiko SFTPClient.
        """
        ssh_config = self._ssh_conf[hostname]
        t = paramiko.Transport((hostname, ssh_config.port))
        t.connect(username=ssh_config.username, pkey=ssh_config.pkey)
        sftp = paramiko.SFTPClient.from_transport(t)
        yield sftp
        sftp.close()
        t.close()

    def remote_exec(self, args, hostname):
        """
        Execute a bash script remotely.

        Args:
            args (list): bash commands
            hostname (str): host name or address

        Returns:
            Process: process handle
        """
        cmd_list = []
        ssh_config = self._ssh_conf[hostname]
        if ssh_config.python_venv:
            cmd_list.append('%s;' % ssh_config.python_venv)
        if ssh_config.env:
            cmd_list.extend(['%s=%s' % (k, v) for k, v in ssh_config.env.items()])
        full_cmd = ' '.join(cmd_list + args)

        remote_cmd = 'ssh -i {} -o StrictHostKeyChecking=no -tt -p {} {}@{} \'bash -c "{}"\'' \
            .format(ssh_config.key_file, ssh_config.port, ssh_config.username, hostname, full_cmd)

        logging.debug('$ %s' % remote_cmd)

        if not ENV.AUTODIST_DEBUG_REMOTE.val:
            # pylint: disable=subprocess-popen-preexec-fn
            proc = subprocess.Popen(remote_cmd, shell=True, preexec_fn=os.setsid,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            return proc
        return None

    def remote_file_write(self, remote_path, data, hostname):
        """
        Write a remote file.

        Args:
            remote_path (str): remote file path
            data (str): data to be written
            hostname (str): host name or address
        """
        with self._get_sftp_client(hostname) as sftp:
            with sftp.open(remote_path, 'w') as f:
                f.write(data)

    def remote_copy(self, local_path, remote_path, hostname):
        """
        Copy a file to a remote directory.

        Args:
            local_path (str): local file path to be copied
            remote_path (str): remote directory path
            hostname (str): host name or address
        """
        # Make sure directory exists
        with self._get_ssh_client(hostname) as client:
            _ = client.exec_command('mkdir -p %s' % remote_path)

        with self._get_sftp_client(hostname) as sftp:
            sftp.put(localpath=local_path, remotepath=os.path.join(remote_path, os.path.basename(local_path)))


def is_loopback_address(address):
    """
    Determine whether an address is a loopback address (e.g. 127.0.0.1).

    Args:
        address (str): Address (can be IP or IP:port)

    Returns:
        Boolean
    """
    ip = _get_ip_from_address(address)
    return ip.is_loopback


def is_local_address(address):
    """
    Determine whether an address is a local (including loopback) IP address.

    Adapted from stackoverflow.com/questions/166506.

    Args:
        address (str): Address (can be IP or IP:port)

    Returns:
        Boolean
    """
    ip = _get_ip_from_address(address)

    # Get all addresses
    addresses = set()
    for iface_name in netifaces.interfaces():
        for i in netifaces.ifaddresses(iface_name).setdefault(netifaces.AF_INET, [{'addr': None}]):
            if i['addr']:
                addresses.add(ip_address(i['addr']))

    return ip in addresses


def _get_ip_from_address(address):
    """
    Extract an IP Address object from an address string.

    Args:
        address (str): Address (can be IP or IP:port)

    Returns:
        An IPv4Address or IPv6Address object.
    """
    ip, _, _ = address.rpartition(':')
    ip = ip or address  # If there was no separation, ip will be empty so use original string
    if ip == 'localhost':
        # These should be equivalent
        # `ip_address` will throw an error if given localhost
        ip = '127.0.0.1'
    return ip_address(ip.strip("[]"))  # IPv6 addresses might contain [] to separate address and port

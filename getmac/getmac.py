# -*- coding: utf-8 -*-

# http://multivax.com/last_question.html

import ctypes
import os
import sys
import struct
import socket
import re
import shlex
import warnings
from subprocess import Popen, PIPE, CalledProcessError
try:
    from subprocess import DEVNULL    # Python 3
except ImportError:
    DEVNULL = open(os.devnull, 'wb')  # Python 2

__version__ = '0.2.0'
DEBUG = False  # TODO

# TODO: better way of doing functions. Lookup table in a dict?


def get_mac_address(interface=None, ip=None, ip6=None,
                    hostname=None, network_request=True):
    """
    Get a Unicast IEEE 802 MAC-48 address from a local interface or remote host.

    You must only use one of the first four arguments. If none of the arguments
    are selected, the default network interface for the system will be used.

    Exceptions will be handled silently and returned as a None.
    For the time being, it assumes you are using Ethernet.

    Args:
        interface (str): Name of a local network interface (e.g "Ethernet 3", "eth0", "ens32")
        ip (str): Canonical dotted decimal IPv4 address of a remote host (e.g 192.168.0.1)
        ip6 (str): Canonical shortened IPv6 address of a remote host (e.g ff02::1:ffe7:7f19)
        hostname (str): DNS hostname of a remote host (e.g "router1.mycorp.com", "localhost")
        network_request (bool): Ping a remote host to populate the ARP/NDP tables for IPv4/IPv6
    Returns:
        Lowercase colon-separated MAC address, or None if one could not be
        found or there was an error.
    """
    mac = None  # MAC address
    funcs = []  # Functions to try using to get a MAC
    arg = None  # Argument to the functions (e.g IP or interface)

    # TODO: move interface here, so we don't try to ping if no IP is set

    # Get the MAC address of a remote host by hostname
    if hostname is not None:
        ip = socket.gethostbyname(hostname)
        # TODO: IPv6 support: use getaddrinfo instead of gethostbyname
        # This would handle case of an IPv6 host

    # Populate the ARP table using a simple ping
    if network_request:
        if sys.platform == 'win32':  # Windows
            _popen("ping", "-n 1 %s" % ip if ip is not None else ip6)
        else:  # Non-Windows
            if ip is not None:  # IPv4
                _popen("ping", "-c 1 %s" % ip)
            else:  # IPv6
                _popen("ping6", "-c 1 %s" % ip6)

    # Get MAC of a IPv4 remote host (or a resolved hostname)
    if ip is not None:
        arg = ip
        if sys.platform == 'win32':  # Windows
            funcs = [_windows_get_remote_mac]
        else:  # Non-Windows
            funcs = [_unix_ip_arp_command, _unix_ip_cat_arp,
                     _unix_ip_ip_command]

    # Get MAC of a IPv6 remote host
    # TODO: "netsh int ipv6 show neigh" (windows cmd)
    elif ip6 is not None:
        if not socket.has_ipv6:
            warnings.warn("Cannot get the MAC address of a IPv6 host: "
                          "IPv6 is not supported on this system",
                          RuntimeWarning)
            return None
        arg = ip6

    # Get MAC of a local interface
    else:
        if interface is not None:
            arg = str(interface)
        # Determine what interface is "default" (has default route)
        elif sys.platform == 'win32':  # Windows
            # TODO: default route OR first interface found windows
            arg = 'Ethernet 1'
        else:  # Non-Windows
            # Try to use the IP command to get default interface
            arg = _unix_default_interface_ip_command()
            if arg is None:
                arg = 'eth0'

        if sys.platform == 'win32':  # Windows
            # _windll_getnode,
            funcs = [_windows_ipconfig_by_interface]
        else:  # Non-Windows
            # _unix_getnode, _unix_arp_by_ip, lanscan_getnode
            funcs = [_unix_ifconfig_by_interface, _unix_interface_ip_command,
                     _unix_netstat_by_interface, _unix_fcntl_by_interface,
                     _unix_lanscan_interface]

    # We try every function and see if it returned a MAC address
    # If it returns None or raises an exception,
    # we continue and try the next function
    for func in funcs:
        try:
            mac = func(arg)
        except Exception as ex:
            if DEBUG:
                print("Exception: %s" % str(ex))  # TODO
                import traceback
                traceback.print_exc()
            continue
        if mac is not None:
            break

    # Make sure address is formatted properly
    if mac is not None:
        # lowercase, colon-separated
        # NOTE: we cast to str ONLY here and NO WHERE ELSE to prevent
        # possibly returning "None" strings.
        mac = str(mac).lower().strip().replace('-', ':').replace(' ', '')

        # Fix cases where there are no colons
        if len(mac) == 12:
            # Source: https://stackoverflow.com/a/3258612/2214380
            mac = ':'.join(mac[i:i + 2] for i in range(0, len(mac), 2))

        # MAC address should ALWAYS be 17 characters with the colons
        if len(mac) != 17:
            mac = None

    return mac


def _windows_get_remote_mac(host):
    # Source: https://goo.gl/ymhZ9p
    # Requires windows 2000 or newer

    # Check for API availability
    try:
        SendARP = ctypes.windll.Iphlpapi.SendARP
    except Exception:
        raise NotImplementedError('Usage only on Windows 2000 and above')

    # Doesn't work with loopbacks, but let's try and help.
    if host == '127.0.0.1' or host.lower() == 'localhost':
        host = socket.gethostname()

    # gethostbyname blocks, so use it wisely.
    try:
        inetaddr = ctypes.windll.wsock32.inet_addr(host)
        if inetaddr in (0, -1):
            raise Exception
    except:
        hostip = socket.gethostbyname(host)
        inetaddr = ctypes.windll.wsock32.inet_addr(hostip)

    buffer = ctypes.c_buffer(6)
    addlen = ctypes.c_ulong(ctypes.sizeof(buffer))

    # TODO: arp_request flag
    if SendARP(inetaddr, 0, ctypes.byref(buffer), ctypes.byref(addlen)) != 0:
        raise WindowsError('Retrieval of mac address(%s) - failed' % host)

    # Convert binary data into a string.
    macaddr = ''
    for intval in struct.unpack('BBBBBB', buffer):
        if intval > 15:
            replacestr = '0x'
        else:
            replacestr = 'x'
        macaddr = ''.join([macaddr, hex(intval).replace(replacestr, '')])
    return macaddr


# TODO: windows remote mac using `arp`, add to README


def _windows_ipconfig_by_interface(interface):
    return _search(re.escape(interface) +
                   r'(?:\n?[^\n]*){1,8}Physical Address.+'
                   r'([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})',
                   _popen('ipconfig', '/all'))


def _unix_fcntl_by_interface(interface):
    # Source: https://stackoverflow.com/a/4789267/2214380
    import fcntl
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # TODO: ip6?
    info = fcntl.ioctl(s.fileno(), 0x8927, struct.pack('256s', interface[:15]))
    return ':'.join(['%02x' % ord(char) for char in info[18:24]])


def _unix_ifconfig_by_interface(interface):
    # This works on Linux ('' or '-a'), Tru64 ('-av'), but not all Unixes.
    for arg in ('', '-a', '-av', '-v'):
        mac = _search(re.escape(interface) +
                      r'.*(HWaddr|Ether) ([0-9a-f]{2}(?::[0-9a-f]{2}){5})',
                      _popen('ifconfig', arg))
        if mac:
            return mac
        else:
            continue
    return None  # TODO: unreachable?


# It would seem that "list" breaks this on Android API 24+ due to SELinux.
# https://github.com/python/cpython/pull/4696/files
# https://bugs.python.org/issue32199
def _unix_interface_ip_command(interface):
    return _search(re.escape(interface) +
                   r'.*\n.*link/ether ([0-9a-f]{2}(?::[0-9a-f]{2}){5})',
                   _popen('ip', 'link'))


def _unix_ip_ip_command(ip):
    pass


def _unix_ip_arp_command(ip):
    return _search(r'\(' + re.escape(ip) +
                   r'\)\s+at\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})',
                   _popen('arp', '-an'))


def _unix_ip_cat_arp(ip):
    return _search(re.escape(ip) + r'.*([0-9a-f]{2}(?::[0-9a-f]{2}){5})',
                   _popen('cat', '/proc/net/arp'))


def _unix_lanscan_interface(interface):
    # Might work on HP-UX
    return _find_mac('lanscan', '-ai', [interface], lambda i: 0)


def _unix_netstat_by_interface(interface):
    return _search(re.escape(interface) +
                   r'.*(HWaddr) ([0-9a-f]{2}(?::[0-9a-f]{2}){5})',
                   _popen('netstat', '-iae'), group_index=1)


def _unix_default_interface_ip_command():
    return _search(r'.*dev ([0-9a-z]*)',
                   _popen('ip', 'route get 0.0.0.0'), group_index=0)


# TODO
def _unix_default_interface_route_command():
    return _search(r'.*(0\.0\.0\.0).*([0-9a-z]*)\n',
                   _popen('route', '-n'), group_index=0)


# TODO: comments
def _search(regex, text, group_index=0):
    match = re.search(regex, text)
    if match:
        return match.groups()[group_index]
    else:
        return None


def _popen(command, args):
    # Try to find the full path to the actual executable of the command
    # This prevents snafus from shell weirdness and other things
    path = os.environ.get("PATH", os.defpath).split(os.pathsep)
    if sys.platform != 'win32':
        path.extend(('/sbin', '/usr/sbin'))  # Add sbin to path on Unix
    for directory in path:
        executable = os.path.join(directory, command)
        if (os.path.exists(executable) and
                os.access(executable, os.F_OK | os.X_OK) and
                not os.path.isdir(executable)):
            break
    else:
        executable = command

    # LC_ALL=C to ensure English output, stderr=DEVNULL to prevent output
    # on stderr (Note: we don't have an example where the words we search
    # for are actually localized, but in theory some system could do so.)
    env = dict(os.environ)
    env['LC_ALL'] = 'C'
    cmd = [executable] + shlex.split(args)
    # Using this instead of check_output is for Python 2.6 compatibility
    process = Popen(cmd, stdout=PIPE, stderr=DEVNULL)
    output, unused_err = process.communicate()
    retcode = process.poll()
    if retcode:
        raise CalledProcessError(retcode, cmd, output=output)
    return str(output)


# TODO: comments
def _find_mac(command, args, hw_identifiers, get_index):
    proc = _popen(command, args)
    for line in proc:
        words = str(line).lower().rstrip().split()
        for i in range(len(words)):
            if words[i] in hw_identifiers:
                word = words[get_index(i)]
                mac = int(word.replace(':', ''), 16)  # b':', b''
                if mac:
                    return mac

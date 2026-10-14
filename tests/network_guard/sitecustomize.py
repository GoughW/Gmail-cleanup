"""Test processes may connect only to loopback; never Google/real OAuth/proxies."""
import ipaddress
import os
import socket

for name in ('GMAIL_CLIENT_ID','GMAIL_CLIENT_SECRET','GMAIL_REFRESH_TOKEN','GMAIL_OAUTH_SCOPE'):
    os.environ.pop(name,None)
_resolve = socket.getaddrinfo
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def local(host):
    if host in ('localhost', b'localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def resolve(host,*args,**kwargs):
    if not local(host):
        raise AssertionError('External DNS/network prohibited by offline test gate')
    return _resolve(host,*args,**kwargs)


def connect(self,address):
    if isinstance(address,tuple) and not local(address[0]):
        raise AssertionError('Non-loopback connection prohibited by offline test gate')
    return _connect(self,address)


def connect_ex(self,address):
    if isinstance(address,tuple) and not local(address[0]):
        raise AssertionError('Non-loopback connection prohibited by offline test gate')
    return _connect_ex(self,address)

socket.getaddrinfo=resolve
socket.socket.connect=connect
socket.socket.connect_ex=connect_ex

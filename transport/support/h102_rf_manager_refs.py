#!/usr/bin/env python3
"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file       h102_rf_manager_refs.py
@brief      Retain exact ownership/guardian unit records through persistent sd-bus references, preventing transient GC identity loss.
@details    Explicit contexts load libsystemd/connect manager; RefUnit is restricted to this session's two units.
            Never create/start/stop/restart services, signal or modify units; closing releases this connection's references automatically.
            Single-thread only, never share forked buses; require Linux libsystemd's public C ABI and reject unsupported candidates.
@author     LogicRF Technologies
@architect  LogicRF
@email      info@logicrf.com
@website    https://www.logicrf.com
@version    0.2.0
@date       2026-10-02
"""
from __future__ import annotations

import ctypes as C
import os
import re


class _UnitRefConnection:
    """Internal native connection; entry points enforce mode allowlists; retain two fixed unit references only.

    Grant no service/device operations. user supports explicit local fixtures only; construction does not connect.
    """
    def __init__(self, allowed, user=False):
        if (type(allowed) is not frozenset or len(allowed)!=2 or
                any(type(unit) is not str or not unit.endswith('.service') for unit in allowed) or
                type(user) is not bool):
            raise ValueError('RF_MANAGER_REF_CONNECTION_INPUTS')
        self.allowed=allowed
        self.user,self.bus,self.lib,self.pid=user,None,None,None
        self.units=set()

    def __enter__(self):
        if self.bus is not None:raise RuntimeError('RF_MANAGER_REF_ALREADY_OPEN')
        lib=C.CDLL('libsystemd.so.0')
        for name in ('sd_bus_open_system','sd_bus_open_user'):
            method=getattr(lib,name);method.argtypes=[C.POINTER(C.c_void_p)];method.restype=C.c_int
        lib.sd_bus_set_method_call_timeout.argtypes=[C.c_void_p,C.c_uint64]
        lib.sd_bus_set_method_call_timeout.restype=C.c_int
        # Pass exactly one c_char_p vararg after eight fixed types to prevent 64-bit pointer truncation.
        lib.sd_bus_call_method.argtypes=[C.c_void_p,C.c_char_p,C.c_char_p,C.c_char_p,C.c_char_p,
                                        C.c_void_p,C.POINTER(C.c_void_p),C.c_char_p]
        lib.sd_bus_call_method.restype=C.c_int
        lib.sd_bus_message_unref.argtypes=[C.c_void_p];lib.sd_bus_message_unref.restype=C.c_void_p
        lib.sd_bus_flush_close_unref.argtypes=[C.c_void_p];lib.sd_bus_flush_close_unref.restype=C.c_void_p
        pointer=C.c_void_p()
        result=getattr(lib,'sd_bus_open_user' if self.user else 'sd_bus_open_system')(C.byref(pointer))
        if result<0:raise OSError(-result,'RF_MANAGER_BUS_OPEN')
        self.lib,self.bus,self.pid=lib,pointer,os.getpid()
        result=lib.sd_bus_set_method_call_timeout(pointer,1_000_000)
        if result<0:
            self.close();raise OSError(-result,'RF_MANAGER_BUS_TIMEOUT')
        return self

    def add(self,unit):
        """Bound RefUnit calls to allowed units; failures never record retained references or relax terminal identity."""
        if unit not in self.allowed:raise ValueError('RF_MANAGER_REF_FOREIGN_UNIT')
        if self.bus is None or self.pid!=os.getpid():raise RuntimeError('RF_MANAGER_REF_CLOSED_OR_FORKED')
        if unit in self.units:return
        reply=C.c_void_p()
        try:
            result=self.lib.sd_bus_call_method(self.bus,b'org.freedesktop.systemd1',
                b'/org/freedesktop/systemd1',b'org.freedesktop.systemd1.Manager',b'RefUnit',
                None,C.byref(reply),b's',C.c_char_p(unit.encode()))
            if result<0:raise OSError(-result,'RF_MANAGER_REF_FAILED: '+unit)
            self.units.add(unit)
        finally:
            if reply.value:self.lib.sd_bus_message_unref(reply)

    def close(self):
        """Close this bus/release its references only; preserve actual service state and failed results.

        Forked children never operate the parent bus; callers cannot inherit this object for service control; parent closes independently.
        """
        if self.bus is not None:
            if self.pid!=os.getpid():raise RuntimeError('RF_MANAGER_REF_FORKED_CLOSE')
            self.lib.sd_bus_flush_close_unref(self.bus)
            self.bus=None;self.units.clear()

    def __exit__(self,*_args):self.close()


class UnitRefs(_UnitRefConnection):
    """Persistent single-session pure-TX references; preserve allowlists/interfaces/main-post responsibilities.

    Accept h102-rf-usb-tx prefixes only, without mixed-session construction/authorization; construction never connects manager.
    """
    def __init__(self,session,user=False):
        if not isinstance(session,str) or not re.fullmatch(r'h102-rf-usb-tx-[a-z0-9-]+',session) or (
                type(user) is not bool):
            raise ValueError('RF_MANAGER_REF_SCOPE')
        super().__init__(frozenset((session+'.service',session+'-guardian.service')),user)


# End of file: h102_rf_manager_refs.py
# Architect : LogicRF
# Contact   : info@logicrf.com
# Website   : https://www.logicrf.com

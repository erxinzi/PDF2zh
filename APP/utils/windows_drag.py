"""Native Windows file drag source; the Shell supplies the actual file object.

Uses SHCreateDataObject + DoDragDrop rather than placing a path on the clipboard.
Only COPY is offered. The source XPI is never moved or installed by this process.
"""
from __future__ import annotations

import ctypes
import os
import uuid
from pathlib import Path
from ctypes import wintypes


class GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def _method(pointer, index, result, *arguments):
    address = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[index]
    return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(address)


def _check(result):
    if result < 0:
        raise OSError(f'Windows 文件拖拽初始化失败（HRESULT 0x{result & 0xffffffff:08X}）')


class ShellFileData:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        if not self.path.is_file():
            raise ValueError('拖拽目标应为插件文件')
        self.pointer = ctypes.c_void_p()
        self.full = ctypes.c_void_p()
        self.parent = None
        self.initialized = False

    def __enter__(self):
        if os.name != 'nt':
            raise OSError('文件拖拽安装当前仅支持 Windows')
        self.ole = ctypes.WinDLL('ole32')
        self.shell = ctypes.WinDLL('shell32')
        self.ole.OleInitialize.argtypes = [ctypes.c_void_p]
        self.ole.OleInitialize.restype = ctypes.c_long
        self.ole.OleUninitialize.argtypes = []
        self.ole.OleUninitialize.restype = None
        self.ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole.CoTaskMemFree.restype = None
        _check(self.ole.OleInitialize(None))
        self.initialized = True
        try:
            self.shell.SHParseDisplayName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            self.shell.SHParseDisplayName.restype = ctypes.c_long
            self.shell.ILClone.argtypes = [ctypes.c_void_p]
            self.shell.ILClone.restype = ctypes.c_void_p
            self.shell.ILRemoveLastID.argtypes = [ctypes.c_void_p]
            self.shell.ILRemoveLastID.restype = wintypes.BOOL
            self.shell.ILFindLastID.argtypes = [ctypes.c_void_p]
            self.shell.ILFindLastID.restype = ctypes.c_void_p
            self.shell.SHCreateDataObject.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
            self.shell.SHCreateDataObject.restype = ctypes.c_long
            attributes = wintypes.DWORD()
            _check(self.shell.SHParseDisplayName(str(self.path), None, ctypes.byref(self.full), 0, ctypes.byref(attributes)))
            self.parent = self.shell.ILClone(self.full)
            if not self.parent:
                raise MemoryError('无法创建插件文件拖拽对象')
            if not self.shell.ILRemoveLastID(self.parent):
                raise OSError('无法定位插件所在目录')
            children = (ctypes.c_void_p * 1)(self.shell.ILFindLastID(self.full))
            iid = GUID.parse('0000010e-0000-0000-C000-000000000046')
            _check(self.shell.SHCreateDataObject(self.parent, 1, children, None, ctypes.byref(iid), ctypes.byref(self.pointer)))
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.pointer.value:
            _method(self.pointer, 2, wintypes.ULONG)(self.pointer)
            self.pointer = ctypes.c_void_p()
        if self.parent:
            self.ole.CoTaskMemFree(self.parent)
            self.parent = None
        if self.full.value:
            self.ole.CoTaskMemFree(self.full)
            self.full = ctypes.c_void_p()
        if self.initialized:
            self.ole.OleUninitialize()
            self.initialized = False


class DropSource:
    def __init__(self):
        HRESULT = ctypes.c_long
        query = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
        ref = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
        keep_dragging = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD)
        feedback = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, wintypes.DWORD)
        class Vtable(ctypes.Structure):
            _fields_ = [('QueryInterface', query), ('AddRef', ref), ('Release', ref),
                        ('QueryContinueDrag', keep_dragging), ('GiveFeedback', feedback)]
        class Interface(ctypes.Structure):
            _fields_ = [('vtable', ctypes.POINTER(Vtable))]
        self.references = 1
        allowed = {uuid.UUID(value).bytes_le for value in ('00000000-0000-0000-C000-000000000046', '00000121-0000-0000-C000-000000000046')}

        def query_interface(_, iid, output):
            if ctypes.string_at(iid, 16) in allowed:
                output[0] = ctypes.addressof(self.interface)
                self.references += 1
                return 0
            output[0] = None
            return -2147467262  # E_NOINTERFACE

        def add_ref(_):
            self.references += 1
            return self.references

        def release(_):
            self.references = max(0, self.references - 1)
            return self.references

        self.vtable = Vtable(query(query_interface), ref(add_ref), ref(release),
                             keep_dragging(lambda _, escape, keys: 0x40101 if escape else (0 if keys & 1 else 0x40100)),
                             feedback(lambda *_: 0x40102))
        self.interface = Interface(ctypes.pointer(self.vtable))


def drag_file(path):
    with ShellFileData(path) as data:
        source = DropSource()
        effect = wintypes.DWORD()
        # HRESULT success codes include both DROP and CANCEL; check explicitly.
        ole = ctypes.WinDLL('ole32')
        ole.DoDragDrop.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        ole.DoDragDrop.restype = ctypes.c_long
        result = ole.DoDragDrop(data.pointer, ctypes.byref(source.interface), 1, ctypes.byref(effect))
        if result < 0:
            raise OSError(f'Windows 文件拖拽失败（HRESULT 0x{result & 0xffffffff:08X}）')
        return result == 0x40100 and bool(effect.value & 1)

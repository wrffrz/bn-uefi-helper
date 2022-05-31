"""
Binary Ninja plugin that aids in analysis of UEFI PEI and DXE modules
"""

import os
import csv
import glob
import uuid
from binaryninja import (BackgroundTaskThread, SegmentFlag, SectionSemantics, BinaryReader, Symbol,
                         SymbolType, HighLevelILOperation, BinaryView, TypeLibrary)
from binaryninja.highlevelil import HighLevelILInstruction
from binaryninja.types import (Type, FunctionParameter)

class UEFIHelper(BackgroundTaskThread):
    """Class for analyzing UEFI firmware to automate GUID annotation, segment fixup, type imports, and more
    """

    def __init__(self, bv: BinaryView):
        BackgroundTaskThread.__init__(self, '', False)
        self.bv = bv
        self.br = BinaryReader(self.bv)
        self.dirname = os.path.dirname(os.path.abspath(__file__))
        self.guids = self._load_guids()
        self.gbs_assignments = []

    def _fix_segments(self):
        """UEFI modules run during boot, without page protections. Everything is RWX despite that the PE is built with
        the segments not being writable. It needs to be RWX so calls through global function pointers are displayed
        properly.
        """

        for seg in self.bv.segments:
            # Make segment RWX
            self.bv.add_user_segment(
                seg.start, seg.data_length, seg.data_offset, seg.data_length,
                SegmentFlag.SegmentWritable|SegmentFlag.SegmentReadable|SegmentFlag.SegmentExecutable
            )

            # Make section semantics ReadWriteDataSectionSemantics
            for section in self.bv.get_sections_at(seg.start):
                self.bv.add_user_section(section.name, section.end-section.start,
                                         SectionSemantics.ReadWriteDataSectionSemantics)

    def _import_types_from_headers(self):
        """Parse EDKII types from header files
        """

        hdrs_path = os.path.join(self.dirname, 'headers')
        typelibraries = glob.glob(os.path.join(hdrs_path, '*.bntl'))
        for tl in typelibraries:
            self.bv.add_type_library(TypeLibrary.load_from_file(tl))
        self.bv.import_library_type("EFI_GUID")
        self.bv.import_library_type("EFI_STATUS")
        self.bv.import_library_type("EFI_HANDLE")
        self.bv.import_library_type("EFI_SYSTEM_TABLE")
        self.bv.import_library_type("EFI_BOOT_SERVICES")
        self.bv.import_library_type("EFI_RUNTIME_SERVICES")
        self.bv.import_library_type("EFI_PEI_FILE_HANDLE")
        self.bv.import_library_type("EFI_PEI_SERVICES")

    def _set_entry_point_prototype(self):
        """Apply correct prototype to the module entry point
        """
        _start = self.bv.get_function_at(self.bv.entry_point)
        if self.bv.view_type != 'TE':
            _start.parameter_vars[0].type = "EFI_HANDLE"
            _start.parameter_vars[0].name = "ImageHandle"
            _start.parameter_vars[1].type = "EFI_SYSTEM_TABLE *"
            _start.parameter_vars[1].name = "SystemTable"

    def _load_guids(self):
        """Read known GUIDs from CSV and convert string GUIDs to bytes

        :return: Dictionary containing GUID bytes and associated names
        """

        guids_path = os.path.join(self.dirname, 'guids.csv')
        with open(guids_path) as f:
            reader = csv.reader(f, skipinitialspace=True)
            guids = dict(reader)

        # Convert to bytes for faster lookup
        guid_bytes = dict()
        for guid, name in guids.items():
            guid_bytes[name] = uuid.UUID(guid).bytes_le

        return guid_bytes

    def _apply_guid_name_if_data(self, name: str, address: int):
        """Check if there is a function at the address. If not, then apply the EFI_GUID type and name it

        :param name: Name/symbol to apply to the GUID
        :param address: Address of the GUID
        """

        print(f'Found {name} at {hex(address)} ({uuid.UUID(bytes_le=self.guids[name])})')

        # Just to avoid a unlikely false positive and screwing up disassembly
        if self.bv.get_functions_at(address) != []:
            print(f'There is code at {address}, not applying GUID type and name')
            return

        self.bv.define_user_symbol(Symbol(SymbolType.DataSymbol, address, 'g'+name))
        t = self.bv.parse_type_string("EFI_GUID")
        self.bv.define_user_data_var(address, t[0])

    def _check_guid_and_get_name(self, guid: bytes) -> str:
        """Check if the GUID is in guids.csv and if it is, return the name

        :param guid: GUID bytes
        :return str: Name of the GUID
        """

        names_list = list(self.guids.keys())
        guids_list = list(self.guids.values())
        try:
            return names_list[guids_list.index(guid)]
        except ValueError:
            return None


    def _find_known_guids(self):
        """Search for known GUIDs and apply names to matches not within a function
        """

        for seg in self.bv.segments:
            for i in range(seg.start, seg.end):
                self.br.seek(i)
                data = self.br.read(16)
                if not data or len(data) != 16:
                    continue

                found_name = self._check_guid_and_get_name(data)
                if found_name:
                    self._apply_guid_name_if_data(found_name, i)

    def _set_if_uefi_core_type(self, instr: HighLevelILInstruction):
        """Using HLIL, scrutinize the instruction to determine if it's a move of a local variable to a global variable.
        If it is, check if the source operand type is a UEFI core type and apply the type to the destination global
        variable.

        :param instr: High level IL instruction object
        """

        print("%x: checking %s" % (instr.address, instr))
        if instr.operation != HighLevelILOperation.HLIL_ASSIGN:
            print("%x: NOT ASSIGN %s" % (instr.address, instr))
            return

        if instr.dest.operation != HighLevelILOperation.HLIL_DEREF:
            print("%x: DEST NOT DEREF %s" % (instr.address, instr.dest))
            return

        if instr.dest.src.operation != HighLevelILOperation.HLIL_CONST_PTR:
            print("%x: DEST.SRC NOT CONST_PTR %s" % (instr.address, instr.dest.src))
            return

        if instr.src.operation != HighLevelILOperation.HLIL_VAR:
            print("%x: SRC NOT VAR %s" % (instr.address, instr.src))
            return

        _type = instr.src.var.type
        print("%x: VAR TYPE %s" % (instr.address, instr.src.var.type))
        if len(_type.tokens) == 1 and str(_type.tokens[0]) == 'EFI_HANDLE':
            self.bv.define_user_symbol(Symbol(SymbolType.DataSymbol, instr.dest.src.constant, 'gHandle'))
        elif len(_type.tokens) == 2 and str(_type.tokens[0]) == 'EFI_BOOT_SERVICES':
            self.bv.define_user_symbol(Symbol(SymbolType.DataSymbol, instr.dest.src.constant, 'gBS'))
            self.gbs_assignments.append(instr.dest.src.constant)
        elif len(_type.tokens) == 2 and str(_type.tokens[0]) == 'EFI_RUNTIME_SERVICES':
            self.bv.define_user_symbol(Symbol(SymbolType.DataSymbol, instr.dest.src.constant, 'gRS'))
        elif len(_type.tokens) == 2 and str(_type.tokens[0]) == 'EFI_SYSTEM_TABLE':
            self.bv.define_user_symbol(Symbol(SymbolType.DataSymbol, instr.dest.src.constant, 'gST'))
        elif len(_type.tokens) == 1 and str(_type.tokens[0]) == 'EFI_PEI_FILE_HANDLE':
            self.bv.define_user_symbol(Symbol(SymbolType.DataSymbol, instr.dest.src.constant, 'gHandle'))
        elif len(_type.tokens) == 2 and str(_type.tokens[0]) == 'EFI_PEI_SERVICES':
            self.bv.define_user_symbol(Symbol(SymbolType.DataSymbol, instr.dest.src.constant, 'gPeiServices'))
        else:
            print("%x: NONE MATCHED %s, %s" % (instr.address, _type.tokens, instr.dest.src.constant))
            return

        self.bv.define_user_data_var(instr.dest.src.constant, instr.src.var.type)
        print(f'Found global assignment - offset:{hex(instr.dest.src.constant)} type:{instr.src.var.type}')

    def _check_and_prop_types_on_call(self, instr: HighLevelILInstruction):
        """Most UEFI modules don't assign globals in the entry function and instead call a initialization routine and
        pass the system table to it where global assignments are made. This function ensures that the types are applied
        to the initialization function params so that we can catch global assignments outside of the module entry

        :param instr: High level IL instruction object
        """

        if instr.operation in [HighLevelILOperation.HLIL_ASSIGN, HighLevelILOperation.HLIL_ASSIGN_UNPACK]:
            instr = instr.src;

        if instr.operation not in [HighLevelILOperation.HLIL_TAILCALL, HighLevelILOperation.HLIL_CALL]:
            return

        if instr.dest.operation != HighLevelILOperation.HLIL_CONST_PTR:
            return

        func = self.bv.get_function_at(instr.dest.constant)
        
        num_params = len(func.parameter_vars)
        for i in range(num_params):
            arg = instr.params[i]
            if hasattr(arg, 'var'):
                typename = arg.var.type
                if "EFI_" in str(typename):
                    func.parameter_vars[i].name = arg.var.name
                    func.parameter_vars[i].type = arg.var.type

    def _set_global_variables(self):
        """On entry, UEFI modules usually set global variables for EFI_BOOT_SERVICES, EFI_RUNTIME_SERIVCES, and
        EFI_SYSTEM_TABLE. This function attempts to identify these assignments and apply types.
        """

        func = self.bv.get_function_at(self.bv.entry_point)
        for block in func.high_level_il:
            for instr in block:
                self._check_and_prop_types_on_call(instr)

        for func in self.bv.functions:
            for block in func.high_level_il:
                for instr in block:
                    self._set_if_uefi_core_type(instr)

    def _name_proto_from_guid(self, guid_addr: int, var_addr: int):
        """Read the GUID, look it up in guids.csv, and derive the var name from the GUID name

        :param guid_addr: Address of GUID
        :param var_addr: Address to create the symbol
        """

        self.br.seek(guid_addr)
        guid = self.br.read(16)
        if len(guid) != 16:
            return

        name = self._check_guid_and_get_name(guid)
        if not name:
            return

        if name.endswith('Guid'):
            name = name.replace('Guid', '')
        print(f'Found {name} at {hex(var_addr)}')
        self.bv.define_user_symbol(Symbol(SymbolType.DataSymbol, var_addr, name))

    def _name_protocol_var(self, instr: HighLevelILInstruction, guid_idx: int, proto_idx: int):
        """Set the global protocol variable name by analyzing the call to gBS->LocateProtocol,
        gBS->InstallMultipleProtocolInterfaces, or gBS->InstallProtocol

        :param instr: HLIL instruction
        :param guid_idx: Param index for the GUID pointer
        :param proto_idx: Param index for the protocol variable pointer
        """

        # Get the largest param index and make sure it doesn't exceed the instruction param count
        param_indices = [guid_idx, proto_idx]
        param_indices.sort()
        if len(instr.params) < param_indices[-1]:
            return

        if instr.params[guid_idx].operation != HighLevelILOperation.HLIL_CONST_PTR:
            return

        if instr.params[proto_idx].operation != HighLevelILOperation.HLIL_CONST_PTR:
            return

        self._name_proto_from_guid(instr.params[guid_idx].constant, instr.params[proto_idx].constant)

    def _name_protocol_vars(self):
        """Iterate xref's for EFI_BOOT_SERVICES global variables, find calls to gBS->LocateProtocol and
        gBS->InstallMultipleProtocolInterfaces, and apply a name and type based on the GUID (if known)
        """

        for assignment in self.gbs_assignments:
            for xref in self.bv.get_code_refs(assignment):
                funcs = self.bv.get_functions_containing(xref.address)
                if not funcs:
                    continue

                for block in funcs[0].high_level_il:
                    for instr in block:
                        if instr.operation != HighLevelILOperation.HLIL_CALL:
                            continue

                        if instr.dest.operation != HighLevelILOperation.HLIL_DEREF_FIELD:
                            continue

                        # Could also use the structure offset or member index here
                        if str(instr.dest).endswith('->LocateProtocol'):
                            self._name_protocol_var(instr, 0, 2)
                        elif str(instr.dest).endswith('->InstallMultipleProtocolInterfaces'):
                            self._name_protocol_var(instr, 1, 2)
                        elif str(instr.dest).endswith('->InstallProtocolInterface'):
                            self._name_protocol_var(instr, 1, 3)

    def run(self):
        """Run the task in the background
        """

        self.progress = "UEFI Helper: Fixing up segments, applying types, and searching for known GUIDs ..."
        self._fix_segments()
        self._import_types_from_headers()
        self._set_entry_point_prototype()
        self._find_known_guids()
        self.progress = "UEFI Helper: searching for global assignments for UEFI core services ..."
        self._set_global_variables()
        self.bv.update_analysis_and_wait()
        self.progress = "UEFI Helper: searching for global protocols ..."
        self._name_protocol_vars()
        print('UEFI Helper completed successfully!')

def run_uefi_helper(bv: BinaryView):
    """Run UEFI helper utilities in the background

    :param bv: BinaryView
    """

    task = UEFIHelper(bv)
    task.start()

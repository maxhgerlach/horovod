# Copyright (C) 2019 Uber Technologies, Inc.
# Modifications copyright Microsoft
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================

import atexit
import ctypes
import os
from typing import Dict, List

from horovod.common import util as util


class HorovodBasics(object):
    """Wrapper class for the basic Horovod API."""

    def __init__(self, pkg_path, *args):
        full_path = util.get_extension_full_path(pkg_path, *args)
        self.MPI_LIB_CTYPES = ctypes.CDLL(full_path, mode=ctypes.RTLD_GLOBAL)

        self.Average = self.MPI_LIB_CTYPES.horovod_reduce_op_average()
        self.Sum = self.MPI_LIB_CTYPES.horovod_reduce_op_sum()
        self.Adasum = self.MPI_LIB_CTYPES.horovod_reduce_op_adasum()

    def init(self, comm=None, process_sets=None):
        """A function that initializes Horovod.

        Args:

          comm: One of these possibilities:

            1) List specifying ranks for the communicator, relative to the MPI_COMM_WORLD
               communicator
            2) None: Use all ranks of MPI_COMM_WORLD
            3) MPI communicator to use. Given communicator will be duplicated.
            4) List of MPI communicators. The first entry will be duplicated and used as
               the global Horovod communicator. Horovod process sets will be built
               corresponding to the remaining entries.

          process_sets: If no explicit MPI communicators are passed, one of these possibilities:

            1) None -- do not initialize any process sets
            2) List[List[int]] -- initialize process sets containing these Horovod ranks
            3) "dynamic": do not initialize any process sets now, but set environment
               HOROVOD_DYNAMIC_PROCESS_SETS=1 .
        """
        if comm is None:
            comm = []
        elif not isinstance(comm, list):
            comm = [comm]
        if process_sets is None:
            process_sets = []
        elif isinstance(process_sets, str) and process_sets.lower() == "dynamic":
            process_sets = []
            os.environ["HOROVOD_DYNAMIC_PROCESS_SETS"] = "1"

        process_set_sizes = [len(ps) for ps in process_sets]
        process_set_ranks = [rank for process_set in process_sets for rank in process_set]
        process_set_args = [
            (ctypes.c_int * len(process_set_ranks))(*process_set_ranks),
            (ctypes.c_int * len(process_set_sizes))(*process_set_sizes),
            ctypes.c_int(len(process_set_sizes))
        ]

        atexit.register(self.shutdown)

        if len(comm) == 0 or all(isinstance(rk, int) for rk in comm):
            comm_size = len(comm)
            self.MPI_LIB_CTYPES.horovod_init(
                (ctypes.c_int * comm_size)(*comm), ctypes.c_int(comm_size),
                *process_set_args)
        else:
            if not self.mpi_built():
                raise ValueError(
                    "Horovod has not been built with MPI support. Ensure MPI is installed and "
                    "reinstall Horovod with HOROVOD_WITH_MPI=1 to debug the build error.")

            from mpi4py import MPI
            if not all(isinstance(c, MPI.Comm) for c in comm):
                raise ValueError(
                    "Invalid type of argument comm. Expected list of rank integers, "
                    "mpi4py.MPI.Comm object or list of mpi4py.MPI.Comm objects.")
            if MPI._sizeof(MPI.Comm) == ctypes.sizeof(ctypes.c_int):
                MPI_Comm = ctypes.c_int
            else:
                MPI_Comm = ctypes.c_void_p

            if len(comm) == 1:
                self.MPI_LIB_CTYPES.horovod_init_comm.argtypes = [MPI_Comm]
                comm_obj = MPI_Comm.from_address(MPI._addressof(comm))
                self.MPI_LIB_CTYPES.horovod_init_comm(comm_obj)
            else:
                comm_objs = [MPI_Comm.from_address(MPI._addressof(c)) for c in comm]
                comm_size = len(comm)
                self.MPI_LIB_CTYPES.horovod_init_multi_comm.argtypes = [MPI_Comm * comm_size, ctypes.c_int]
                self.MPI_LIB_CTYPES.horovod_init_multi_comm((MPI_Comm * comm_size)(*comm_objs),
                                                            ctypes.c_int(comm_size))

    def shutdown(self):
        """A function that shuts Horovod down."""
        self.MPI_LIB_CTYPES.horovod_shutdown()

    def is_initialized(self):
        """Returns True if Horovod is initialized"""
        return self.MPI_LIB_CTYPES.horovod_is_initialized()

    def start_timeline(self, file_path, mark_cycles=False):
        """Creates a timeline file at `file_path` and begins recording.

        Args:
            file_path: String path to the timeline file.
            mark_cycles: Boolean indicating that cycles should be marked on
                         the timeline (default: False).

        Raises a `ValueError` if Horovod is not initialized.
        """
        result = self.MPI_LIB_CTYPES.horovod_start_timeline(
            ctypes.c_char_p(file_path.encode('utf-8')),
            ctypes.c_bool(mark_cycles))
        if result == -1:
            raise ValueError('Horovod has not been initialized; use hvd.init().')
        elif result == -2:
            raise ValueError('Set HOROVOD_TIMELINE=DYNAMIC to enable dynamic timeline usage.');

    def stop_timeline(self):
        """Stops the active timeline recording and closes the file.

        Raises a `ValueError` if Horovod is not initialized.
        """
        result = self.MPI_LIB_CTYPES.horovod_stop_timeline()
        if result == -1:
            raise ValueError('Horovod has not been initialized; use hvd.init().')

    def size(self):
        """A function that returns the number of Horovod processes.

        Returns:
          An integer scalar containing the number of Horovod processes.
        """
        size = self.MPI_LIB_CTYPES.horovod_size()
        if size == -1:
            raise ValueError(
                'Horovod has not been initialized; use hvd.init().')
        return size

    def local_size(self):
        """A function that returns the number of Horovod processes within the
        node the current process is running on.

        Returns:
          An integer scalar containing the number of local Horovod processes.
        """
        local_size = self.MPI_LIB_CTYPES.horovod_local_size()
        if local_size == -1:
            raise ValueError(
                'Horovod has not been initialized; use hvd.init().')
        return local_size

    def cross_size(self):
        """A function that returns the number of nodes for the local rank of the current
        Horovod process. For example, if there are 2 nodes in the job: one running 2 processes
        and the other running 1 process, then the first process on each node will have cross
        size 2, and the second process on the first node will have cross size 1.

        Returns:
          An integer scalar containing the number of cross Horovod processes.
        """
        cross_size = self.MPI_LIB_CTYPES.horovod_cross_size()
        if cross_size == -1:
            raise ValueError(
                'Horovod has not been initialized; use hvd.init().')
        return cross_size

    def rank(self):
        """A function that returns the Horovod rank of the calling process.

        Returns:
          An integer scalar with the Horovod rank of the calling process.
        """
        rank = self.MPI_LIB_CTYPES.horovod_rank()
        if rank == -1:
            raise ValueError(
                'Horovod has not been initialized; use hvd.init().')
        return rank

    def local_rank(self):
        """A function that returns the local Horovod rank of the calling process, within the
        node that it is running on. For example, if there are seven processes running
        on a node, their local ranks will be zero through six, inclusive.

        Returns:
          An integer scalar with the local Horovod rank of the calling process.
        """
        local_rank = self.MPI_LIB_CTYPES.horovod_local_rank()
        if local_rank == -1:
            raise ValueError(
                'Horovod has not been initialized; use hvd.init().')
        return local_rank

    def cross_rank(self):
        """A function that returns the cross Horovod rank of the calling process, across the
        nodes in the job. The cross rank of a process corresponds to the rank of the node its
        is running on. For example, if there are 7 nodes in a job, the cross ranks will be
        zero through six, inclusive.

        Returns:
          An integer scalar with the cross Horovod rank of the calling process.
        """
        cross_rank = self.MPI_LIB_CTYPES.horovod_cross_rank()
        if cross_rank == -1:
            raise ValueError(
                'Horovod has not been initialized; use hvd.init().')
        return cross_rank

    def is_homogeneous(self):
        """Returns True if the cluster is homogeneous.

        Returns:
          A boolean value indicating whether every node in the cluster has same number of ranks.
        """
        is_homogeneous = self.MPI_LIB_CTYPES.horovod_is_homogeneous()
        return bool(is_homogeneous)

    def mpi_threads_supported(self):
        """A function that returns a flag indicating whether MPI multi-threading is supported.

        If MPI multi-threading is supported, users may mix and match Horovod usage with other
        MPI libraries, such as `mpi4py`.

        Returns:
          A boolean value indicating whether MPI multi-threading is supported.
        """
        mpi_enabled = self.MPI_LIB_CTYPES.horovod_mpi_enabled()
        if not bool(mpi_enabled):
            raise ValueError(
                'Horovod MPI is not enabled; Please make sure it\'s installed and enabled.')

        mpi_threads_supported = self.MPI_LIB_CTYPES.horovod_mpi_threads_supported()
        if mpi_threads_supported == -1:
            raise ValueError(
                'Horovod has not been initialized; use hvd.init().')
        return bool(mpi_threads_supported)

    def mpi_enabled(self):
        """Returns True if MPI is mode is currently enabled at runtime.

        If MPI is enabled, users can use it for controller or data transfer operations.

        Returns:
          A boolean value indicating whether MPI is enabled.
        """
        mpi_enabled = self.MPI_LIB_CTYPES.horovod_mpi_enabled()
        return bool(mpi_enabled)

    def mpi_built(self):
        """Returns True if Horovod was compiled with MPI support.

        Returns:
          A boolean value indicating whether MPI support was compiled.
        """
        return bool(self.MPI_LIB_CTYPES.horovod_mpi_built())

    def gloo_enabled(self):
        """Returns True if Gloo is mode is currently enabled at runtime.

        If Gloo is enabled, users can use it for controller or data transfer operations.

        Returns:
          A boolean value indicating whether Gloo is enabled.
        """
        gloo_enabled = self.MPI_LIB_CTYPES.horovod_gloo_enabled()
        return bool(gloo_enabled)

    def gloo_built(self):
        """Returns True if Horovod was compiled with Gloo support.

        Returns:
          A boolean value indicating whether Gloo support was compiled.
        """
        return bool(self.MPI_LIB_CTYPES.horovod_gloo_built())

    def nccl_built(self):
        """Function to check if Horovod was compiled with NCCL support.

        Returns:
          An integer value indicating whether NCCL support was compiled.
          If NCCL support was compiled, returns NCCL_VERSION_CODE. Otherwise,
          returns 0.
        """
        return int(self.MPI_LIB_CTYPES.horovod_nccl_built())

    def ddl_built(self):
        """Returns True if Horovod was compiled with DDL support.

        Returns:
          A boolean value indicating whether DDL support was compiled.
        """
        return bool(self.MPI_LIB_CTYPES.horovod_ddl_built())

    def ccl_built(self):
        """Returns True if Horovod was compiled with oneCCL support.

        Returns:
          A boolean value indicating whether oneCCL support was compiled.
        """
        return bool(self.MPI_LIB_CTYPES.horovod_ccl_built())

    def cuda_built(self):
        """Returns True if Horovod was compiled with CUDA support.

        Returns:
          A boolean value indicating whether CUDA support was compiled.
        """
        return bool(self.MPI_LIB_CTYPES.horovod_cuda_built())

    def rocm_built(self):
        """Returns True if Horovod was compiled with ROCm support.

        Returns:
          A boolean value indicating whether ROCm support was compiled.
        """
        return bool(self.MPI_LIB_CTYPES.horovod_rocm_built())

    def add_process_set(self, ranks):
        """ Add a new process set and return its id.

        Requires running with HOROVOD_DYNAMIC_PROCESS_SETS=1.
        """
        if not isinstance(ranks, list):
            ranks = list(ranks)
        nrank = len(ranks)
        result = int(self.MPI_LIB_CTYPES.horovod_add_process_set(
            (ctypes.c_int * nrank)(*ranks), ctypes.c_int(nrank)))
        if result == -1:
            raise ValueError('Horovod has not been initialized; use hvd.init().')
        elif result == -2:
            raise ValueError(
                "Set HOROVOD_DYNAMIC_PROCESS_SETS=1 to allow adding process sets after Horovod initialization.")
        elif result == -10:
            raise ValueError("Multiple process sets are only supported with MPI controllers, not Gloo.")
        return result

    def remove_process_set(self, process_set_id):
        """ Remove process set with given id.

        Requires running with HOROVOD_DYNAMIC_PROCESS_SETS=1.
        """
        assert isinstance(process_set_id, int)
        if process_set_id == 0:
            raise ValueError("Attempted to remove the global process set with id 0.")
        result = int(self.MPI_LIB_CTYPES.horovod_remove_process_set(
            ctypes.c_int(process_set_id)))
        if result == -1:
            raise ValueError('Horovod has not been initialized; use hvd.init().')
        elif result == -2:
            raise ValueError(
                "Set HOROVOD_DYNAMIC_PROCESS_SETS=1 to allow removing process sets after Horovod initialization.")
        elif result == -3:
            raise ValueError(f"Tried to remove unknown process set id {process_set_id}")
        elif result == -10:
            raise ValueError("Multiple process sets are only supported with MPI controllers, not Gloo.")
        return result

    def process_set_rank(self, process_set_id):
        """ Return process rank relative to the process set. """
        assert isinstance(process_set_id, int)
        result = int(self.MPI_LIB_CTYPES.horovod_process_set_rank(
            ctypes.c_int(process_set_id)))
        if result == -1:
            raise ValueError('Horovod has not been initialized; use hvd.init().')
        elif result == -2:
            raise ValueError(f"Process is not part of process set {process_set_id}")
        elif result == -3:
            raise ValueError(f"Unknown process set id {process_set_id}")
        elif result == -10:
            raise ValueError("Multiple process sets are only supported with MPI controllers, not Gloo.")
        return result

    def process_set_size(self, process_set_id):
        """ Return size of the process set. """
        assert isinstance(process_set_id, int)
        result = int(self.MPI_LIB_CTYPES.horovod_process_set_size(
            ctypes.c_int(process_set_id)))
        if result == -1:
            raise ValueError('Horovod has not been initialized; use hvd.init().')
        elif result == -2:
            raise ValueError(f"Process is not part of process set {process_set_id}")
        elif result == -3:
            raise ValueError(f"Unknown process set id {process_set_id}")
        elif result == -10:
            raise ValueError("Multiple process sets are only supported with MPI controllers, not Gloo.")
        return result

    def get_process_sets(self) -> Dict[int, List[int]]:
        """ Returns a dictionary { process_set_id: list of process set ranks } """
        num = int(self.MPI_LIB_CTYPES.horovod_get_number_of_process_sets())
        ids_array = (ctypes.c_int * num)()
        self.MPI_LIB_CTYPES.horovod_get_process_set_ids(ids_array)
        ret = {}
        for ps_id in ids_array:
            ps_size = int(self.MPI_LIB_CTYPES.horovod_get_process_set_size(ctypes.c_int(ps_id)))
            if ps_size < 0:
                raise RuntimeError("Process set table was modified outside of get_process_sets()")
            ranks_array = (ctypes.c_int * ps_size)()
            res = int(self.MPI_LIB_CTYPES.horovod_get_process_set_ranks(ctypes.c_int(ps_id), ranks_array))
            if res < 0:
                raise RuntimeError("Process set table was modified outside of get_process_sets()")
            ret[ps_id] = list(ranks_array)
        return ret

    def comm_process_set(self, comm) -> int:
        """ Returns the (previously registered) process set id corresponding to the MPI communicator comm. """
        if not self.mpi_built():
            raise ValueError(
                "Horovod has not been built with MPI support. Ensure MPI is installed and "
                "reinstall Horovod with HOROVOD_WITH_MPI=1 to debug the build error.")

        from mpi4py import MPI
        if MPI._sizeof(MPI.Comm) == ctypes.sizeof(ctypes.c_int):
            MPI_Comm = ctypes.c_int
        else:
            MPI_Comm = ctypes.c_void_p

        self.MPI_LIB_CTYPES.horovod_comm_process_set.argtypes = [MPI_Comm]
        comm_obj = MPI_Comm.from_address(MPI._addressof(comm))
        result = int(self.MPI_LIB_CTYPES.horovod_comm_process_set(comm_obj))
        if result == -1:
            raise ValueError('Horovod has not been initialized or MPI has not been enabled; use hvd.init().')
        elif result == -2:
            raise ValueError('MPI communicator does not correspond to any registered process set.')
        return result

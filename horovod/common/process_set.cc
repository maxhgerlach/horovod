#include <algorithm>
#include <utility>

#include "controller.h"
#include "process_set.h"

namespace horovod {
namespace common {

ProcessSet::ProcessSet(std::vector<int> global_ranks)
    : registered_global_ranks_(std::move(global_ranks)) {}

bool ProcessSet::IsCurrentProcessIncluded() const {
  // TODO: maybe switch this to checking the global-rank -> rank mapping
  assert(initialization_done);
  return controller->IsInitialized();
}

#if HAVE_MPI
void ProcessSet::Initialize(const MPIContext& mpi_context) {
  if (initialization_done) {
    return;
  }
  LOG(TRACE) << "Initializing new process set with MPI.";
  assert(controller != nullptr);
  if (!registered_global_ranks_.empty()) {
    // Verify that each process has registered the same set of processes.
    int size;
    MPI_Comm_size(mpi_context.global_comm, &size);
    std::vector<int> buf(size);
    assert(registered_global_ranks_.size() <= size);
    auto len = static_cast<int>(registered_global_ranks_.size());
    MPI_Allgather(&len, 1, MPI_INT, buf.data(), 1, MPI_INT,
                  mpi_context.global_comm);
    if (std::any_of(buf.begin(), buf.end(), [len](int other_len) {
          return len != other_len;
        })) {
      throw std::logic_error("Attempted to register process set with "
                             "mismatching size on different ranks");
    }
    for (auto reduction_op : {MPI_MAX, MPI_MIN}) {
      buf.resize(len);
      MPI_Allreduce(registered_global_ranks_.data(), buf.data(), len, MPI_INT,
                    reduction_op, mpi_context.global_comm);
      if (registered_global_ranks_ != buf) {
        throw std::logic_error("Attempted to register process set with "
                               "mismatching values on different ranks");
      }
    }
  }
  mpi_comms.Initialize(mpi_context, registered_global_ranks_);
  if (mpi_comms.Get(CommunicatorType::GLOBAL) != MPI_COMM_NULL) {
    // The running process is part of this process set.
    controller->Initialize();
  }
  initialization_done = true;
}
#endif // HAVE_MPI

#if HAVE_GLOO
void ProcessSet::Initialize(const GlooContext& gloo_context) {
  if (initialization_done) {
    return;
  }
  assert(controller != nullptr);
  controller->Initialize();  // TODO: only initialize controller if this process belongs to the process set
  initialization_done = true;
}
#endif // HAVE_GLOO

void ProcessSet::Finalize(const Status& status) {
  tensor_queue.FinalizeTensorQueue(status);
#if HAVE_MPI
  mpi_comms.Finalize();
#endif // HAVE_MPI
  initialization_done = false;
}

ProcessSetTable::ProcessSetTable() {
  auto process_set_id = RegisterProcessSet();
  assert(process_set_id == 0);
}

#if HAVE_MPI
void ProcessSetTable::Initialize(const MPIContext& mpi_context) {
  std::lock_guard<std::recursive_mutex> guard(mutex);
  assert(next_id_ == 1);  // exactly one process set is registered
  Get(0).Initialize(mpi_context);
}

void ProcessSetTable::InitializeRegisteredIfReady(const MPIContext& mpi_context) {
  std::lock_guard<std::recursive_mutex> guard(mutex);

  int locally_registered_count = ids_.size();
  auto& global_controller = *Get(0).controller;
  auto registered_counts = std::vector<int>(global_controller.GetSize());
  global_controller.AllgatherInt(locally_registered_count, registered_counts);
  if (std::any_of(registered_counts.begin(), registered_counts.end(),
                  [locally_registered_count](int reg_count) {
                    return reg_count != locally_registered_count;
                  })) {
    // Do not initialize newly added process sets until every process has
    // registered them.
    return;
  }

  for (auto id: Ids()) {
    Get(id).Initialize(mpi_context);
  }
}
#endif // HAVE_MPI

#if HAVE_GLOO
void ProcessSetTable::Initialize(const GlooContext& gloo_context) {
  std::lock_guard<std::recursive_mutex> guard(mutex);
  assert(next_id_ == 1);  // exactly one process set is registered
  Get(0).Initialize(gloo_context);
}
#endif // HAVE_GLOO

void ProcessSetTable::Finalize(const Status& status) {
  std::lock_guard<std::recursive_mutex> guard(mutex);
  std::vector<int32_t> ids_copy(ids_.begin(), ids_.end());
  for (auto id: ids_copy) {
    id_to_process_set_[id].Finalize(status);
    if (id != 0) {
      // The process set hosting the global controller needs to remain in the
      // table to allow a future re-initialization of Horovod (it must still
      // be finalized now and re-initialized then).
      DeregisterProcessSet(id);
    }
  }
}

int32_t ProcessSetTable::RegisterProcessSet(std::vector<int> global_ranks) {
  std::lock_guard<std::recursive_mutex> guard(mutex);

  if (!global_ranks.empty() && Contains(0)) {
    // We are registering a potentially non-global process set and we have
    // already registered the global process set 0. -> Check global_ranks
    std::sort(global_ranks.begin(), global_ranks.end());
    auto dup_it = std::adjacent_find(global_ranks.begin(), global_ranks.end());
    if (dup_it != global_ranks.end()) {
      throw std::logic_error(
          "Tried to register process set with duplicate rank: " +
          std::to_string(*dup_it));
    }
    for (auto rk : global_ranks) {
      if (rk < 0 || rk >= Get(0).controller->GetSize()) {
        throw std::logic_error(
            "Tried to register process set with invalid rank: " +
            std::to_string(rk));
      }
    }
  }

  int32_t id;
  if (!free_ids_.empty()) {
    id = free_ids_.front();
    free_ids_.pop();
  } else {
    id = next_id_++;
  }

  // emplace (id, ProcessSet()) without requiring a copy constructor (would be
  // nicer in C++17 with try_emplace)
  id_to_process_set_.emplace(std::piecewise_construct,
                             std::forward_as_tuple(id),
                             std::forward_as_tuple(global_ranks));
  ids_.push_back(id);

  return id;
}

void ProcessSetTable::DeregisterProcessSet(int32_t process_set_id) {
  std::lock_guard<std::recursive_mutex> guard(mutex);
  auto map_it = id_to_process_set_.find(process_set_id);
  if (map_it != id_to_process_set_.end()) {
    id_to_process_set_.erase(map_it);

    auto ids_it = std::find(ids_.begin(), ids_.end(), process_set_id);
    if (ids_it != ids_.end()) {
      ids_.erase(ids_it);
    }

    free_ids_.push(process_set_id);
  }
}
std::vector<int32_t> ProcessSetTable::Ids() const {
  std::lock_guard<std::recursive_mutex> guard(mutex);
  return ids_;
}

bool ProcessSetTable::Contains(int32_t id) const {
  std::lock_guard<std::recursive_mutex> guard(mutex);
  return id_to_process_set_.find(id) != id_to_process_set_.end();
}

ProcessSet& ProcessSetTable::Get(int32_t id) {
  std::lock_guard<std::recursive_mutex> guard(mutex);
  return id_to_process_set_.at(id);
}

void ProcessSetTable::MarkProcessSetForRemoval(int32_t process_set_id) {
  std::lock_guard<std::recursive_mutex> guard(mutex);
  assert(id_to_be_removed_ == NO_PENDING_REMOVAL);
  id_to_be_removed_ = process_set_id;
}

bool ProcessSetTable::ProcessSetHasJustBeenRemoved() {
  std::lock_guard<std::recursive_mutex> guard(mutex);
  if (id_to_be_removed_ == SUCCESSFUL_REMOVAL) {
    id_to_be_removed_ = NO_PENDING_REMOVAL;
    return true;
  }
  return false;
}

void ProcessSetTable::RemoveMarkedProcessSetIfReady() {
  std::lock_guard<std::recursive_mutex> guard(mutex);

  auto& global_controller = *Get(0).controller;
  auto ids_marked_on_all_ranks = std::vector<int>(global_controller.GetSize());
  global_controller.AllgatherInt(id_to_be_removed_, ids_marked_on_all_ranks);
  if (std::any_of(
          ids_marked_on_all_ranks.begin(), ids_marked_on_all_ranks.end(),
          [this](int other_id) { return other_id != id_to_be_removed_; })) {
    // Do not remove marked process set until every process has marked the same.
    return;
  }
  if (id_to_be_removed_ == NO_PENDING_REMOVAL ||
      id_to_be_removed_ == SUCCESSFUL_REMOVAL) {
    return;
  }

  id_to_process_set_[id_to_be_removed_].Finalize(
      Status::Aborted("Process set has been removed"));
  DeregisterProcessSet(id_to_be_removed_);

  id_to_be_removed_ = SUCCESSFUL_REMOVAL;
}

} // common
} // horovod
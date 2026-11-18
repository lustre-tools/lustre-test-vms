rdma_provider(shmrdma
  shmrdma.c
  )

# ibv_cmd_get_context took the fds to pass to the kernel in v61
file(READ "${CMAKE_SOURCE_DIR}/libibverbs/driver.h" SHM_DRIVER_H)
if (SHM_DRIVER_H MATCHES "struct ibv_fd_arr \\*fd_arr")
  target_compile_definitions("shmrdma-rdmav${IBVERBS_PABI_VERSION}"
    PRIVATE HAVE_GET_CONTEXT_FD_ARR)
endif()

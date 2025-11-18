# Linux Debugging Results: Futex Orphaned Lock Analysis

Analysis of GDB output from child process hang after fork.

## Thread State

From line 176:

```
* 1    Thread 0xf8ada4747020 (LWP 24) "python3"
```

Only one thread exists in the child process.

## Stack Trace

From lines 178-210:

```
#0  0x0000f8ada3f2bb24 in syscall () from /lib/aarch64-linux-gnu/libc.so.6
#1  0x0000f8ad919212a8 in ?? () from chromadb_rust_bindings.abi3.so
#2-8  chromadb_rust_bindings.abi3.so
#9-30 Python interpreter (libpython3.12.so.1.0)
```

Call chain: Python interpreter -> ChromaDB Rust bindings -> syscall function in libc.

## Register Analysis

From lines 212-251, key registers:

```
x8  = 0x62                (98 decimal)
x0  = 0xb51ef13a3b20      (199144500771616)
x1  = 0x89                (137 decimal)
x2  = 0x21                (33 decimal)
x3  = 0x0
x5  = 0xffffffff          (4294967295)
pc  = 0xf8ada3f2bb24      <syscall+36>
```

### Syscall Identification

From [futex(2) man page](https://man7.org/linux/man-pages/man2/futex.2.html), the syscall signature is:

```c
long syscall(SYS_futex, uint32_t *uaddr, int op, ...);
```

On ARM64, syscall arguments are passed in registers x0-x5, with x8 (w8 in 32-bit context) holding the syscall number ([syscall(2) man page](https://man7.org/linux/man-pages/man2/syscall.2.html#NOTES), [Chromium OS syscall table](https://www.chromium.org/chromium-os/developer-library/reference/linux-constants/syscalls/)).

**From [futex(2) man page](https://man7.org/linux/man-pages/man2/futex.2.html):**

> "The uaddr argument points to the futex word. On all platforms, futexes are four-byte integers that must be aligned on a four-byte boundary. The operation to perform on the futex is specified in the op argument."

> "When executing a futex operation that requests to block a thread, the kernel will block only if the futex word has the value that the calling thread supplied (as one of the arguments of the futex() call) as the expected value of the futex word."

`x8 = 98`:
- Syscall number register
- 98 = `__NR_futex` (`#define __NR_futex 98` from [unistd.h](https://github.com/torvalds/linux/blob/master/include/uapi/asm-generic/unistd.h))

`x0 = 0xb51ef13a3b20`:
- First argument: `uaddr` (futex address)
- Memory address of the futex word

`x1 = 137`:
- Second argument: `op` (operation)
- 137 decimal = 0x89 hex
- Interpreting as `FUTEX_WAIT_BITSET_PRIVATE` from [futex.h](https://github.com/torvalds/linux/blob/master/include/uapi/linux/futex.h):
  - `FUTEX_WAIT_BITSET` = 9
  - `FUTEX_PRIVATE_FLAG` = 128 (0x80)
  - 9 | 128 = 137 (0x89)

`x2 = 33`:
- Third argument: expected value
- Process waits if futex word equals this value

`x3 = 0`:
- Fourth argument: timeout
- NULL (0x0) = wait indefinitely

`x5 = 0xffffffff`:
- Sixth argument: bitset for `FUTEX_WAIT_BITSET`
- 0xffffffff = `FUTEX_BITSET_MATCH_ANY` ([futex.h](https://github.com/torvalds/linux/blob/master/include/uapi/linux/futex.h))

`pc = 0xf8ada3f2bb24`:
- Program counter at `syscall+36`
- Process frozen at this instruction

### Reconstructed Syscall

```c
futex(0xb51ef13a3b20,           // uaddr
      FUTEX_WAIT_BITSET_PRIVATE, // op (137)
      33,                         // val
      NULL,                       // timeout
      NULL,                       // uaddr2
      0xffffffff)                 // val3
```

## Disassembly Analysis

From lines 261-277:

```asm
   0x0000f8ada3f2bb04 <+4>:     mov     w8, w0
   0x0000f8ada3f2bb08 <+8>:     mov     x0, x1
   0x0000f8ada3f2bb0c <+12>:    mov     x1, x2
   0x0000f8ada3f2bb10 <+16>:    mov     x2, x3
   0x0000f8ada3f2bb14 <+20>:    mov     x3, x4
   0x0000f8ada3f2bb18 <+24>:    mov     x4, x5
   0x0000f8ada3f2bb1c <+28>:    mov     x5, x6
   0x0000f8ada3f2bb20 <+32>:    mov     x6, x7
=> 0x0000f8ada3f2bb24 <+36>:    svc     #0x0
   0x0000f8ada3f2bb28 <+40>:    cmn     x0, #0xfff
   0x0000f8ada3f2bb2c <+44>:    b.cs    0xf8ada3f2bb34
   0x0000f8ada3f2bb30 <+48>:    ret
```

From [GDB documentation](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Machine-Code.html):
> "If the range of memory being disassembled contains current program counter, the instruction at that location is shown with a => marker."

**svc #0x0 instruction**: From the instruction's position in the `syscall()` function (line 272) and register state showing a futex syscall, the process appears blocked in kernel space.

**Lines +40, +44, +48**:
- Never reached
- Would execute after syscall returns
- Process never returns from kernel

## What This Means

The child process is deadlocked on an orphaned lock:

1. **Only one thread exists** (line 176) but the futex was likely locked by a thread in the parent process that doesn't exist in the child after fork.

2. **Waiting on futex syscall** (x8 = 98, x1 = 137) specifically `FUTEX_WAIT_BITSET_PRIVATE`, waiting for the futex word at `0xb51ef13a3b20` to change from value 33.

3. **No timeout** (x3 = 0x0) will wait indefinitely for the futex to be released.

4. **Process is frozen in kernel** (pc at `svc` instruction in `syscall()`) the syscall has transferred control to the kernel, which will never return because no thread exists to release the lock.

**The deadlock**: The futex is marked as "locked" in memory (value 33), but the thread that locked it existed only in the parent process. After fork, that thread is gone. The child's single thread is now waiting for a lock that will never be released, because the thread that holds it doesn't exist.

## Sources

- [Linux futex(2) man page](https://man7.org/linux/man-pages/man2/futex.2.html) - Futex system call documentation
- [Linux syscall(2) man page](https://man7.org/linux/man-pages/man2/syscall.2.html) - System call calling conventions
- [Linux kernel unistd.h](https://github.com/torvalds/linux/blob/master/include/uapi/asm-generic/unistd.h) - Syscall number definitions (`__NR_futex`)
- [Linux kernel futex.h](https://github.com/torvalds/linux/blob/master/include/uapi/linux/futex.h) - Futex operation flags and constants
- [Chromium OS syscall table](https://www.chromium.org/chromium-os/developer-library/reference/linux-constants/syscalls/) - ARM64 syscall calling convention reference
- [GDB documentation](https://sourceware.org/gdb/current/onlinedocs/gdb.html/) - GNU Debugger manual

## Full Output

```bash
❯ docker compose run --rm crash-gdb
================================================================================
ATTACHING GDB TO CHILD PROCESS (Linux)
================================================================================
Started parent PID: 10
Waiting for child process...
Found child PID via psutil: 24

Waiting for SIGUSR1 signal from child...
(Child will signal when embeddings are done, ChromaDB deadlock is next)

  Received SIGUSR1 - embeddings done

Attaching gdb to child PID 24...
(Should catch ChromaDB deadlock now)


[INFO] GDB timed out - process likely hung (deadlock)
This is expected Linux behavior - mutex deadlock instead of crash

=== DETAILED BACKTRACE (process hung) ===
[Thread debugging using libthread_db enabled]
Using host libthread_db library "/lib/aarch64-linux-gnu/libthread_db.so.1".
0x0000f8ada3f2bb24 in syscall () from /lib/aarch64-linux-gnu/libc.so.6
  Id   Target Id                                Frame 
* 1    Thread 0xf8ada4747020 (LWP 24) "python3" 0x0000f8ada3f2bb24 in syscall () from /lib/aarch64-linux-gnu/libc.so.6

Thread 1 (Thread 0xf8ada4747020 (LWP 24) "python3"):
#0  0x0000f8ada3f2bb24 in syscall () from /lib/aarch64-linux-gnu/libc.so.6
#1  0x0000f8ad919212a8 in ?? () from /usr/local/lib/python3.12/site-packages/chromadb_rust_bindings/chromadb_rust_bindings.abi3.so
#2  0x0000f8ad90eea974 in ?? () from /usr/local/lib/python3.12/site-packages/chromadb_rust_bindings/chromadb_rust_bindings.abi3.so
#3  0x0000f8ad9100e59c in ?? () from /usr/local/lib/python3.12/site-packages/chromadb_rust_bindings/chromadb_rust_bindings.abi3.so
#4  0x0000f8ad910cf6d8 in ?? () from /usr/local/lib/python3.12/site-packages/chromadb_rust_bindings/chromadb_rust_bindings.abi3.so
#5  0x0000f8ad9112e52c in ?? () from /usr/local/lib/python3.12/site-packages/chromadb_rust_bindings/chromadb_rust_bindings.abi3.so
#6  0x0000f8ad91137a60 in ?? () from /usr/local/lib/python3.12/site-packages/chromadb_rust_bindings/chromadb_rust_bindings.abi3.so
#7  0x0000f8ad91122d7c in ?? () from /usr/local/lib/python3.12/site-packages/chromadb_rust_bindings/chromadb_rust_bindings.abi3.so
#8  0x0000f8ad91130c78 in ?? () from /usr/local/lib/python3.12/site-packages/chromadb_rust_bindings/chromadb_rust_bindings.abi3.so
#9  0x0000f8ada422f5d0 in ?? () from /usr/local/bin/../lib/libpython3.12.so.1.0
#10 0x0000f8ada419ee48 [PAC] in PyObject_Vectorcall () from /usr/local/bin/../lib/libpython3.12.so.1.0
#11 0x0000f8ada41864f0 [PAC] in _PyEval_EvalFrameDefault () from /usr/local/bin/../lib/libpython3.12.so.1.0
#12 0x0000f8ada41c91b4 [PAC] in ?? () from /usr/local/bin/../lib/libpython3.12.so.1.0
#13 0x0000f8ada41b35ec [PAC] in ?? () from /usr/local/bin/../lib/libpython3.12.so.1.0
#14 0x0000f8ada4189fb0 [PAC] in _PyEval_EvalFrameDefault () from /usr/local/bin/../lib/libpython3.12.so.1.0
#15 0x0000f8ada4179874 [PAC] in _PyObject_FastCallDictTstate () from /usr/local/bin/../lib/libpython3.12.so.1.0
#16 0x0000f8ada41af8d0 [PAC] in ?? () from /usr/local/bin/../lib/libpython3.12.so.1.0
#17 0x0000f8ada4175fc8 [PAC] in ?? () from /usr/local/bin/../lib/libpython3.12.so.1.0
#18 0x0000f8ada4175ce8 [PAC] in _PyObject_MakeTpCall () from /usr/local/bin/../lib/libpython3.12.so.1.0
#19 0x0000f8ada41864f0 [PAC] in _PyEval_EvalFrameDefault () from /usr/local/bin/../lib/libpython3.12.so.1.0
#20 0x0000f8ada42269c8 [PAC] in PyEval_EvalCode () from /usr/local/bin/../lib/libpython3.12.so.1.0
#21 0x0000f8ada427e1b0 [PAC] in ?? () from /usr/local/bin/../lib/libpython3.12.so.1.0
#22 0x0000f8ada4276648 [PAC] in ?? () from /usr/local/bin/../lib/libpython3.12.so.1.0
#23 0x0000f8ada4271b08 [PAC] in ?? () from /usr/local/bin/../lib/libpython3.12.so.1.0
#24 0x0000f8ada42710d4 [PAC] in _PyRun_SimpleFileObject () from /usr/local/bin/../lib/libpython3.12.so.1.0
#25 0x0000f8ada4270d40 [PAC] in _PyRun_AnyFileObject () from /usr/local/bin/../lib/libpython3.12.so.1.0
#26 0x0000f8ada426b614 [PAC] in Py_RunMain () from /usr/local/bin/../lib/libpython3.12.so.1.0
#27 0x0000f8ada4209640 [PAC] in Py_BytesMain () from /usr/local/bin/../lib/libpython3.12.so.1.0
#28 0x0000f8ada3e6229c [PAC] in ?? () from /lib/aarch64-linux-gnu/libc.so.6
#29 0x0000f8ada3e6237c [PAC] in __libc_start_main () from /lib/aarch64-linux-gnu/libc.so.6
#30 0x0000b51ed1d10930 [PAC] in _start ()

Thread 1 (Thread 0xf8ada4747020 (LWP 24) "python3"):
x0             0xb51ef13a3b20      199144500771616
x1             0x89                137
x2             0x21                33
x3             0x0                 0
x4             0x0                 0
x5             0xffffffff          4294967295
x6             0x0                 0
x7             0x0                 0
x8             0x62                98
x9             0x2                 2
x10            0x2                 2
x11            0x0                 0
x12            0x0                 0
x13            0x3b9aca00          1000000000
x14            0x18                24
x15            0xb51ef13d4960      199144500971872
x16            0xf8ad91d861f0      273424359907824
x17            0xf8ada3f2bb00      273424663624448
x18            0xfffff10f1c48      281474726042696
x19            0xb51ef13a3b18      199144500771608
x20            0xb51ef13a3b10      199144500771600
x21            0x0                 0
x22            0x21                33
x23            0xf8ad91da1a88      273424360020616
x24            0xfffff10f17b8      281474726041528
x25            0xfffff10f1958      281474726041944
x26            0xeaecb7ec          3941382124
x27            0x8000000000000000  -9223372036854775808
x28            0x1                 1
x29            0xfffff10fb140      281474726080832
x30            0xf8ad919212a8      273424355300008
sp             0xfffff10f17b0      0xfffff10f17b0
pc             0xf8ada3f2bb24      0xf8ada3f2bb24 <syscall+36>
cpsr           0x60001000          [ EL=0 BTYPE=0 SSBS C Z ]
fpsr           0x15                [ IOC OFC IXC ]
fpcr           0x0                 [ Len=0 Stride=0 RMode=0 ]
tpidr          0xf8ada4747740      0xf8ada4747740
tpidr2         0x0                 0x0
pauth_dmask    0x7f000000000000    35747322042253312
pauth_cmask    0x7f000000000000    35747322042253312

Thread 1 (Thread 0xf8ada4747020 (LWP 24) "python3"):
$1 = 199144500771616

Thread 1 (Thread 0xf8ada4747020 (LWP 24) "python3"):
$2 = 137

Thread 1 (Thread 0xf8ada4747020 (LWP 24) "python3"):
$3 = 33
Dump of assembler code for function syscall:
   0x0000f8ada3f2bb00 <+0>:     bti     c
   0x0000f8ada3f2bb04 <+4>:     mov     w8, w0
   0x0000f8ada3f2bb08 <+8>:     mov     x0, x1
   0x0000f8ada3f2bb0c <+12>:    mov     x1, x2
   0x0000f8ada3f2bb10 <+16>:    mov     x2, x3
   0x0000f8ada3f2bb14 <+20>:    mov     x3, x4
   0x0000f8ada3f2bb18 <+24>:    mov     x4, x5
   0x0000f8ada3f2bb1c <+28>:    mov     x5, x6
   0x0000f8ada3f2bb20 <+32>:    mov     x6, x7
=> 0x0000f8ada3f2bb24 <+36>:    svc     #0x0
   0x0000f8ada3f2bb28 <+40>:    cmn     x0, #0xfff
   0x0000f8ada3f2bb2c <+44>:    b.cs    0xf8ada3f2bb34 <syscall+52>  // b.hs, b.nlast
   0x0000f8ada3f2bb30 <+48>:    ret
   0x0000f8ada3f2bb34 <+52>:    b       0xf8ada3e62440
   0x0000f8ada3f2bb38 <+56>:    b       0xf8ada3e62440
End of assembler dump.
A debugging session is active.

        Inferior 1 [process 24] will be detached.

Quit anyway? (y or n) [answered Y; input not from terminal]
[Inferior 1 (process 24) detached]
```



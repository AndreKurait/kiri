//! Compile Docker/OCI JSON profiles before clone. libseccomp resolves syscall
//! names and emits architecture-correct BPF; the child only installs that BPF.
use std::{ffi::{CString, c_void, c_char, c_int}, io::{self, Read, Seek}, os::fd::{AsRawFd, FromRawFd}};
use serde_json::Value;

#[repr(C)]
struct Arg { index: u32, op: u32, a: u64, b: u64 }
#[link(name = "seccomp", kind = "static")]
unsafe extern "C" {
    fn seccomp_init(action: u32) -> *mut c_void;
    fn seccomp_release(ctx: *mut c_void);
    fn seccomp_arch_resolve_name(name: *const c_char) -> u32;
    fn seccomp_arch_add(ctx: *mut c_void, arch: u32) -> c_int;
    fn seccomp_syscall_resolve_name(name: *const c_char) -> c_int;
    fn seccomp_rule_add_array(ctx: *mut c_void, action: u32, syscall: c_int, count: u32, args: *const Arg) -> c_int;
    fn seccomp_export_bpf(ctx: *mut c_void, fd: c_int) -> c_int;
}
struct Context(*mut c_void);
impl Drop for Context { fn drop(&mut self) { unsafe { seccomp_release(self.0) }; } }
pub struct Filter(Vec<libc::sock_filter>);
fn bad(message: impl ToString) -> io::Error { io::Error::new(io::ErrorKind::InvalidInput, message.to_string()) }
fn check(rc: c_int) -> io::Result<()> { if rc < 0 { Err(io::Error::from_raw_os_error(-rc)) } else { Ok(()) } }
fn action(name: &str, errno: Option<u64>) -> io::Result<u32> {
    let data = errno.unwrap_or(libc::EPERM as u64);
    if data > 65535 { return Err(bad("seccomp errno exceeds 16 bits")); }
    Ok(match name {
        "SCMP_ACT_KILL" | "SCMP_ACT_KILL_THREAD" => 0,
        "SCMP_ACT_KILL_PROCESS" => 0x80000000,
        "SCMP_ACT_TRAP" => 0x00030000,
        "SCMP_ACT_ERRNO" => 0x00050000 | data as u32,
        "SCMP_ACT_TRACE" => 0x7ff00000 | data as u32,
        "SCMP_ACT_LOG" => 0x7ffc0000,
        "SCMP_ACT_ALLOW" => 0x7fff0000,
        _ => return Err(bad(format!("unsupported seccomp action {name}"))),
    })
}
fn arch(name: &str) -> io::Result<u32> {
    let name = CString::new(name.strip_prefix("SCMP_ARCH_").unwrap_or(name).to_lowercase()).map_err(bad)?;
    let token = unsafe { seccomp_arch_resolve_name(name.as_ptr()) };
    if token == 0 { Err(bad("invalid seccomp architecture")) } else { Ok(token) }
}
const CAPS: &[&str] = &["CHOWN", "DAC_OVERRIDE", "DAC_READ_SEARCH", "FOWNER", "FSETID", "KILL", "SETGID", "SETUID", "SETPCAP", "LINUX_IMMUTABLE", "NET_BIND_SERVICE", "NET_BROADCAST", "NET_ADMIN", "NET_RAW", "IPC_LOCK", "IPC_OWNER", "SYS_MODULE", "SYS_RAWIO", "SYS_CHROOT", "SYS_PTRACE", "SYS_PACCT", "SYS_ADMIN", "SYS_BOOT", "SYS_NICE", "SYS_RESOURCE", "SYS_TIME", "SYS_TTY_CONFIG", "MKNOD", "LEASE", "AUDIT_WRITE", "AUDIT_CONTROL", "SETFCAP", "MAC_OVERRIDE", "MAC_ADMIN", "SYSLOG", "WAKE_ALARM", "BLOCK_SUSPEND", "AUDIT_READ", "PERFMON", "BPF", "CHECKPOINT_RESTORE"];
fn selected(rule: &Value, caps: u64) -> io::Result<bool> {
    let native = if cfg!(target_arch = "x86_64") { "amd64" } else { "arm64" };
    for (field, exclude) in [("includes", false), ("excludes", true)] {
        let condition = &rule[field];
        if condition.is_null() { continue; }
        if let Some(arches) = condition["arches"].as_array() {
            let matched = arches.iter().any(|a| a.as_str() == Some(native));
            if (exclude && matched) || (!exclude && !arches.is_empty() && !matched) { return Ok(false); }
        }
        if let Some(required) = condition["caps"].as_array() {
            let mut matches = Vec::new();
            for cap in required {
                let name = cap.as_str().ok_or_else(|| bad("invalid seccomp capability"))?.trim_start_matches("CAP_");
                let bit = CAPS.iter().position(|n| *n == name).ok_or_else(|| bad(format!("unknown capability {name}")))?;
                matches.push(caps & (1u64 << bit) != 0);
            }
            if (exclude && matches.iter().any(|m| *m)) || (!exclude && matches.iter().any(|m| !*m)) { return Ok(false); }
        }
        if !condition["minKernel"].is_null() {
            let want = condition["minKernel"].as_str().ok_or_else(|| bad("invalid minKernel"))?;
            let parse = |s: &str| s.split('.').take(2).map(|v| v.parse::<u32>().unwrap_or(0)).collect::<Vec<_>>();
            let current = std::fs::read_to_string("/proc/sys/kernel/osrelease")?;
            let matched = parse(&current) >= parse(want);
            if matched == exclude { return Ok(false); }
        }
    }
    Ok(true)
}
pub fn compile(json: &str, caps: u64) -> io::Result<Filter> {
    let profile: Value = serde_json::from_str(json).map_err(bad)?;
    if profile["flags"].as_array().is_some_and(|v| !v.is_empty()) || !profile["listenerPath"].is_null() || !profile["listenerMetadata"].is_null() {
        return Err(bad("seccomp listener and explicit filter flags are not supported"));
    }
    let default = action(profile["defaultAction"].as_str().ok_or_else(|| bad("missing defaultAction"))?, profile["defaultErrnoRet"].as_u64())?;
    let ctx = Context(unsafe { seccomp_init(default) });
    if ctx.0.is_null() { return Err(bad("cannot initialize seccomp profile")); }
    for a in profile["architectures"].as_array().into_iter().flatten() {
        let token = arch(a.as_str().ok_or_else(|| bad("invalid architecture"))?)?;
        let rc = unsafe { seccomp_arch_add(ctx.0, token) };
        if rc != -libc::EEXIST { check(rc)?; }
    }
    let native = if cfg!(target_arch = "x86_64") { "SCMP_ARCH_X86_64" } else { "SCMP_ARCH_AARCH64" };
    for mapping in profile["archMap"].as_array().into_iter().flatten() {
        if mapping["architecture"] == native {
            for a in mapping["subArchitectures"].as_array().into_iter().flatten() {
                let rc = unsafe { seccomp_arch_add(ctx.0, arch(a.as_str().ok_or_else(|| bad("invalid subarchitecture"))?)?) };
                if rc != -libc::EEXIST { check(rc)?; }
            }
        }
    }
    for rule in profile["syscalls"].as_array().into_iter().flatten() {
        if !selected(rule, caps)? { continue; }
        let act = action(rule["action"].as_str().ok_or_else(|| bad("missing syscall action"))?, rule["errnoRet"].as_u64())?;
        if act == default { continue; }
        let mut args = Vec::new();
        for a in rule["args"].as_array().into_iter().flatten() {
            let index = a["index"].as_u64().filter(|n| *n < 6).ok_or_else(|| bad("invalid syscall argument index"))? as u32;
            let op = match a["op"].as_str().unwrap_or_default() {
                "SCMP_CMP_NE" => 1, "SCMP_CMP_LT" => 2, "SCMP_CMP_LE" => 3, "SCMP_CMP_EQ" => 4,
                "SCMP_CMP_GE" => 5, "SCMP_CMP_GT" => 6, "SCMP_CMP_MASKED_EQ" => 7,
                _ => return Err(bad("invalid syscall argument operator")),
            };
            args.push(Arg { index, op, a: a["value"].as_u64().ok_or_else(|| bad("invalid argument value"))?, b: a["valueTwo"].as_u64().unwrap_or(0) });
        }
        let names = rule["names"].as_array().ok_or_else(|| bad("missing syscall names"))?;
        for name in names {
            let name = CString::new(name.as_str().ok_or_else(|| bad("invalid syscall name"))?).map_err(bad)?;
            let nr = unsafe { seccomp_syscall_resolve_name(name.as_ptr()) };
            if nr == -1 { continue; } // Profiles can include syscalls absent on this architecture.
            check(unsafe { seccomp_rule_add_array(ctx.0, act, nr, args.len() as u32, args.as_ptr()) })?;
        }
    }
    let fd = unsafe { libc::memfd_create(c"seccomp".as_ptr(), libc::MFD_CLOEXEC) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    let mut file = unsafe { std::fs::File::from_raw_fd(fd) };
    check(unsafe { seccomp_export_bpf(ctx.0, file.as_raw_fd()) })?;
    file.rewind()?;
    let mut bytes = Vec::new(); file.read_to_end(&mut bytes)?;
    if bytes.len() % 8 != 0 || bytes.len() / 8 > 4096 { return Err(bad("invalid BPF size")); }
    let insns = bytes.chunks_exact(8).map(|b| libc::sock_filter { code: u16::from_ne_bytes([b[0], b[1]]), jt: b[2], jf: b[3], k: u32::from_ne_bytes([b[4], b[5], b[6], b[7]]) }).collect();
    Ok(Filter(insns))
}
impl Filter {
    pub fn install(&self) -> io::Result<()> {
        let prog = libc::sock_fprog { len: self.0.len() as u16, filter: self.0.as_ptr() as *mut _ };
        let rc = unsafe { libc::syscall(libc::SYS_seccomp, 1, 0, &prog) };
        if rc < 0 { Err(io::Error::last_os_error()) } else { Ok(()) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_invalid_profiles_and_argument_indices() {
        assert!(compile("{}", 0).is_err());
        assert!(compile(r#"{"defaultAction":"SCMP_ACT_ALLOW","architectures":["bogus"]}"#, 0).is_err());
        assert!(compile(r#"{"defaultAction":"SCMP_ACT_ALLOW","syscalls":[{"names":["getppid"],"action":"SCMP_ACT_ERRNO","args":[{"index":6,"op":"SCMP_CMP_EQ","value":0}]}]}"#, 0).is_err());
    }

    #[test]
    fn custom_filter_is_enforced_by_the_kernel() {
        let filter = compile(r#"{"defaultAction":"SCMP_ACT_ALLOW","syscalls":[{"names":["getppid"],"action":"SCMP_ACT_ERRNO","errnoRet":13}]}"#, 0).unwrap();
        // Keep enforcement inside the child so the test runner does not
        // inherit either filter; exit without re-entering the test framework.
        let pid = unsafe { libc::fork() };
        assert!(pid >= 0);
        if pid == 0 {
            unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) };
            if super::super::seccomp::install(None).is_err() { unsafe { libc::_exit(4) }; }
            if filter.install().is_err() { unsafe { libc::_exit(2) }; }
            let denied = unsafe { libc::syscall(libc::SYS_getppid) } == -1 && io::Error::last_os_error().raw_os_error() == Some(13);
            let boundary = unsafe { libc::socket(libc::AF_VSOCK, libc::SOCK_STREAM, 0) } == -1 && io::Error::last_os_error().raw_os_error() == Some(libc::EAFNOSUPPORT);
            unsafe { libc::_exit(if denied && boundary { 0 } else { 3 }) };
        }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(pid, &mut status, 0) }, pid);
        assert!(libc::WIFEXITED(status) && libc::WEXITSTATUS(status) == 0, "child status: {status}");
    }
}

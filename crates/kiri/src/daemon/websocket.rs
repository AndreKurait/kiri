//! Docker's WebSocket attach: binary messages, no Docker stream multiplexing.
//! A socket pair wakes poll on container output; there is no latency timer in
//! the data path. Slow clients apply backpressure to the forwarding thread.
use std::io::{self, BufReader, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::sync::{Arc, atomic::{AtomicBool, Ordering}, mpsc};
use std::time::Duration;

use kiri_proto::Stream;
use tungstenite::{Message, WebSocket, protocol::Role};

use super::{api::{Next, error}, engine::{Engine, Logs}, http::{self, Request}};

enum Output {
    Live(mpsc::Receiver<(Stream, Vec<u8>)>),
    Logs(Logs),
    Empty,
}

pub(super) fn attach(req: &Request, engine: &Arc<Engine>, key: &str, w: &mut UnixStream, r: &mut BufReader<UnixStream>) -> io::Result<Next> {
    let key_header = req.header("sec-websocket-key").unwrap_or_default();
    let upgrade = req.header("upgrade").is_some_and(|v| v.eq_ignore_ascii_case("websocket"));
    let connection = req.header("connection").is_some_and(|v| v.split(',').any(|v| v.trim().eq_ignore_ascii_case("upgrade")));
    if !upgrade || !connection || req.header("sec-websocket-version") != Some("13")
        || kiri_image::b64::decode(key_header).is_none_or(|key| key.len() != 16) {
        http::error(w, 400, "invalid WebSocket upgrade request")?;
        return Ok(Next::Close);
    }
    let id = match engine.resolve(key) {
        Ok(id) => id,
        Err(e) => { error(w, e)?; return Ok(Next::KeepAlive); }
    };
    let output = if req.flag("logs") {
        match engine.logs(&id, req.flag("stream")) {
            Ok(Some(logs)) => Output::Logs(logs),
            Ok(None) => {
                http::error(w, 501, "configured logging driver does not support reading")?;
                return Ok(Next::Close);
            }
            Err(e) => { error(w, e)?; return Ok(Next::KeepAlive); }
        }
    } else if req.flag("stream") {
        match engine.attach(&id) {
            Ok((_, _, rx)) => Output::Live(rx),
            Err(e) => { error(w, e)?; return Ok(Next::KeepAlive); }
        }
    } else {
        Output::Empty
    };
    let accept = tungstenite::handshake::derive_accept_key(key_header.as_bytes());
    http::respond_with(w, 101, "application/octet-stream",
        &format!("Connection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: {accept}\r\n"), b"")?;
    let early = r.buffer().to_vec();
    let stdin = req.flag("stdin");
    let result = bridge(w.try_clone()?, early, output, req.flag("stdout"), req.flag("stderr"), |data| {
        if stdin { engine.input(&id, data).map_err(io::Error::other) } else { Ok(()) }
    });
    if stdin {
        let _ = engine.close_stdin(&id);
    }
    result?;
    Ok(Next::Close)
}

struct Stop(Arc<AtomicBool>);
impl Drop for Stop {
    fn drop(&mut self) { self.0.store(true, Ordering::Relaxed); }
}

fn forward(mut pipe: UnixStream, output: Output, stop: &AtomicBool, stdout: bool, stderr: bool) {
    let mut send = |stream: Stream, bytes: &[u8]| -> io::Result<()> {
        let wanted = match stream { Stream::Stdout => stdout, Stream::Stderr => stderr };
        if wanted && !bytes.is_empty() {
            // Bounded frames also bound the WebSocket write buffer.
            for chunk in bytes.chunks(32 * 1024) {
                pipe.write_all(&(chunk.len() as u32).to_be_bytes())?;
                pipe.write_all(chunk)?;
            }
        }
        Ok(())
    };
    match output {
        Output::Live(rx) => while !stop.load(Ordering::Relaxed) {
            match rx.recv_timeout(Duration::from_millis(100)) {
                Ok((stream, bytes)) => if send(stream, &bytes).is_err() { break; },
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
                Err(mpsc::RecvTimeoutError::Timeout) => {}
            }
        },
        Output::Logs(logs) => {
            for record in logs.past.tail(None) {
                if stop.load(Ordering::Relaxed) || send(record.stream, &record.data).is_err() { return; }
            }
            if let Some(rx) = logs.live {
                while !stop.load(Ordering::Relaxed) {
                    match rx.recv_timeout(Duration::from_millis(100)) {
                        Ok(record) => if send(record.stream, &record.data).is_err() { break; },
                        Err(mpsc::RecvTimeoutError::Disconnected) => break,
                        Err(mpsc::RecvTimeoutError::Timeout) => {}
                    }
                }
            }
        }
        Output::Empty => {}
    }
}

fn blocked(error: &tungstenite::Error) -> bool {
    matches!(error, tungstenite::Error::Io(e) if e.kind() == io::ErrorKind::WouldBlock)
}

fn bridge(stream: UnixStream, early: Vec<u8>, output: Output, stdout: bool, stderr: bool, mut input: impl FnMut(&[u8]) -> io::Result<()>) -> io::Result<()> {
    let (mut incoming, outgoing) = UnixStream::pair()?;
    let stop = Stop(Arc::new(AtomicBool::new(false)));
    let stopping = stop.0.clone();
    std::thread::Builder::new().name("kiri-websocket-output".into()).spawn(move || forward(outgoing, output, &stopping, stdout, stderr))?;
    stream.set_nonblocking(true)?;
    let mut ws = WebSocket::from_partially_read(stream, early, Role::Server, None);
    let mut closing = false;
    loop {
        // Read buffered frames too: a client can send input immediately behind
        // its HTTP upgrade request.
        if !closing {
            loop {
                match ws.read() {
                    Ok(Message::Binary(bytes)) => input(&bytes)?,
                    Ok(Message::Text(text)) => input(text.as_bytes())?,
                    Ok(Message::Close(_)) => { closing = true; break; }
                    Ok(_) => {} // tungstenite queues replies to Ping automatically.
                    Err(e) if blocked(&e) => break,
                    Err(tungstenite::Error::ConnectionClosed) => return Ok(()),
                    Err(e) => return Err(io::Error::other(e)),
                }
            }
        }
        let pending = match ws.flush() {
            Ok(()) if closing => return Ok(()),
            Ok(()) => false,
            Err(e) if blocked(&e) => true,
            Err(tungstenite::Error::ConnectionClosed) => return Ok(()),
            Err(e) => return Err(io::Error::other(e)),
        };
        let mut fds = [
            libc::pollfd { fd: ws.get_ref().as_raw_fd(), events: libc::POLLIN | if pending { libc::POLLOUT } else { 0 }, revents: 0 },
            libc::pollfd { fd: if pending || closing { -1 } else { incoming.as_raw_fd() }, events: libc::POLLIN, revents: 0 },
        ];
        // SAFETY: two initialized pollfd entries, valid for this call.
        if unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as _, -1) } < 0 {
            let e = io::Error::last_os_error();
            if e.kind() == io::ErrorKind::Interrupted { continue; }
            return Err(e);
        }
        if fds[1].revents != 0 {
            let mut length = [0; 4];
            match incoming.read_exact(&mut length) {
                Ok(()) => {
                    let mut bytes = vec![0; u32::from_be_bytes(length) as usize];
                    incoming.read_exact(&mut bytes)?;
                    if let Err(e) = ws.send(Message::Binary(bytes.into())) {
                        if !blocked(&e) { return Err(io::Error::other(e)); }
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => {
                    closing = true;
                    if let Err(e) = ws.close(None) {
                        if !blocked(&e) { return Err(io::Error::other(e)); }
                    }
                }
                Err(e) => return Err(e),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bidirectional_binary_frames_ping_and_close() {
        let (client, server) = UnixStream::pair().unwrap();
        client.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let (tx, rx) = mpsc::channel();
        let (received, input) = mpsc::channel();
        let worker = std::thread::spawn(move || bridge(server, vec![], Output::Live(rx), true, false, |data| {
            received.send(data.to_vec()).unwrap();
            Ok(())
        }));
        let mut ws = WebSocket::from_raw_socket(client, Role::Client, None);
        ws.send(Message::Binary(b"stdin".to_vec().into())).unwrap();
        assert_eq!(input.recv_timeout(Duration::from_secs(3)).unwrap(), b"stdin");
        ws.send(Message::Ping(b"alive".to_vec().into())).unwrap();
        assert_eq!(ws.read().unwrap(), Message::Pong(b"alive".to_vec().into()));
        tx.send((Stream::Stderr, b"hidden".to_vec())).unwrap();
        tx.send((Stream::Stdout, b"stdout".to_vec())).unwrap();
        assert_eq!(ws.read().unwrap(), Message::Binary(b"stdout".to_vec().into()));
        drop(tx);
        assert!(matches!(ws.read().unwrap(), Message::Close(_)));
        worker.join().unwrap().unwrap();
    }
}

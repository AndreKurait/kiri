//! Docker detach sequences, including sequences split between transport frames.
pub(super) struct Keys {
    sequence: Vec<u8>,
    pending: Vec<u8>,
}

impl Keys {
    pub(super) fn parse(value: Option<&str>) -> Result<Self, &'static str> {
        let value = value.filter(|v| !v.is_empty()).unwrap_or("ctrl-p,ctrl-q");
        let mut sequence = Vec::new();
        if !value.is_empty() {
            for key in value.split(',') {
                let byte = if key.len() == 1 {
                    key.as_bytes()[0]
                } else if let Some(control) = key.strip_prefix("ctrl-") {
                    match control.as_bytes() {
                        [b'a'..=b'z'] => control.as_bytes()[0] - b'a' + 1,
                        [b'@'] => 0,
                        [b'['] => 27,
                        [b'\\'] => 28,
                        [b']'] => 29,
                        [b'^'] => 30,
                        [b'_'] => 31,
                        _ => return Err("invalid detach keys"),
                    }
                } else {
                    return Err("invalid detach keys");
                };
                if !byte.is_ascii() { return Err("invalid detach keys"); }
                sequence.push(byte);
            }
        }
        Ok(Self { sequence, pending: Vec::new() })
    }

    pub(super) fn finish(&mut self) -> Vec<u8> { std::mem::take(&mut self.pending) }

    pub(super) fn feed(&mut self, input: &[u8]) -> (Vec<u8>, bool) {
        if self.sequence.is_empty() { return (input.to_vec(), false); }
        let mut output = Vec::with_capacity(input.len());
        for &byte in input {
            if self.pending.is_empty() && byte != self.sequence[0] { output.push(byte); continue; }
            self.pending.push(byte);
            while !self.sequence.starts_with(&self.pending) {
                output.push(self.pending.remove(0));
            }
            if self.pending == self.sequence {
                self.pending.clear();
                return (output, true);
            }
        }
        (output, false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn partial_and_overlapping_sequences_preserve_input() {
        let mut keys = Keys::parse(Some("a,a,b")).unwrap();
        assert_eq!(keys.feed(b"za"), (b"z".to_vec(), false));
        assert_eq!(keys.feed(b"aac"), (b"aaac".to_vec(), false));
        assert_eq!(keys.feed(b"aa"), (vec![], false));
        assert_eq!(keys.feed(b"bignored"), (vec![], true));
        let mut keys = Keys::parse(None).unwrap();
        assert_eq!(keys.feed(&[16]), (vec![], false));
        assert_eq!(keys.feed(&[17]), (vec![], true));
    }

    #[test]
    fn invalid_and_default_sequences() {
        assert!(Keys::parse(Some("ctrl-foo")).is_err());
        assert!(Keys::parse(Some("ctrl-p,")).is_err());
        assert_eq!(Keys::parse(Some("")).unwrap().feed(&[16, 17]), (vec![], true));
        let mut keys = Keys::parse(None).unwrap();
        keys.feed(&[16]);
        assert_eq!(keys.finish(), vec![16]);
    }
}

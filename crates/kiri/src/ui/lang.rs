//! The window's language: the system's, or the one picked in Settings →
//! Appearance (the `lang` setting: "system", "en" or "zh-Hans").
//!
//! The page does its own words (index.html's `tr()`): it gets the
//! translations spliced in by assemble.rs and the system's languages from
//! [`boot_js`]. The few words drawn natively (the Mac's menus, the Windows
//! tray menu) come from [`tr`], out of the same files. English is the key, so
//! a word missing from a file stays English. The Mac and Linux window and
//! kiri-win (by `#[path]`) share this file.

use std::collections::HashMap;
use std::sync::OnceLock;

/// Every translation Kiri ships, by language tag, as lang/<tag>.json maps
/// English to it. English itself has no file.
pub const FILES: [(&str, &str); 1] = [("zh-Hans", include_str!("lang/zh-Hans.json"))];

/// The files as one JS object literal, `{"zh-Hans": {...}}`, for the page.
pub fn strings_js() -> String {
    let mut out = String::from("{");
    for (i, (tag, json)) in FILES.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str(&serde_json::Value::from(*tag).to_string());
        out.push(':');
        out.push_str(json.trim());
    }
    out.push('}');
    out
}

/// A language tag to one Kiri has, as the page's `langFor` does: any
/// Chinese reads Simplified until there is a Traditional file.
fn lang_for(tag: &str) -> Option<&'static str> {
    let t = tag.to_ascii_lowercase().replace('_', "-");
    if t.starts_with("zh") {
        return Some("zh-Hans");
    }
    if t == "en" || t.starts_with("en-") {
        return Some("en");
    }
    FILES.iter().map(|(id, _)| *id).find(|id| {
        let id = id.to_ascii_lowercase();
        t == id || t.starts_with(&format!("{id}-"))
    })
}

/// The language the window shows for the `lang` setting.
pub fn resolve(pick: Option<&str>) -> &'static str {
    match pick {
        Some(p) if !p.is_empty() && p != "system" => lang_for(p).unwrap_or("en"),
        _ => os_languages().iter().find_map(|l| lang_for(l)).unwrap_or("en"),
    }
}

/// Before the page's script: the system's languages and the saved pick, so
/// the first paint is already in the right language.
pub fn boot_js(pick: Option<&str>) -> String {
    let v = serde_json::json!({ "os": os_languages(), "pick": pick.unwrap_or("system") });
    format!("window.KIRI_LANG = {v};")
}

/// `en` in `lang`, for words drawn natively; English when there's no translation.
pub fn tr(lang: &str, en: &str) -> String {
    static MAPS: OnceLock<HashMap<&'static str, HashMap<String, String>>> = OnceLock::new();
    let maps = MAPS.get_or_init(|| FILES.iter().map(|(tag, json)| (*tag, serde_json::from_str(json).unwrap_or_default())).collect());
    maps.get(lang).and_then(|m| m.get(en)).cloned().unwrap_or_else(|| en.to_owned())
}

/// `{name}` placeholders in `s` filled from `vars`, as the page's tr() does:
/// in one pass, so a value that holds braces (an error's text) stays as it is.
pub fn fill(s: String, vars: &[(&str, &dyn std::fmt::Display)]) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s.as_str();
    while let Some(open) = rest.find('{') {
        out.push_str(&rest[..open]);
        let after = &rest[open + 1..];
        let name = after.find('}').map(|close| &after[..close]).filter(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_'));
        match name.and_then(|n| vars.iter().find(|(k, _)| *k == n)) {
            Some((k, v)) => {
                out.push_str(&v.to_string());
                rest = &after[k.len() + 1..];
            }
            None => {
                out.push('{');
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

/// The system's preferred languages, most preferred first, as BCP 47 tags.
#[cfg(target_os = "macos")]
pub fn os_languages() -> Vec<String> {
    use objc2::rc::Retained;
    use objc2::runtime::AnyObject;
    use objc2::{class, msg_send};
    use objc2_foundation::NSString;
    // SAFETY: +[NSLocale preferredLanguages] returns an NSArray of NSString,
    // read with plain count/objectAtIndex: on any thread.
    unsafe {
        let Some(list): Option<Retained<AnyObject>> = msg_send![class!(NSLocale), preferredLanguages] else { return Vec::new() };
        let n: usize = msg_send![&list, count];
        (0..n)
            .filter_map(|i| {
                let s: Option<Retained<NSString>> = msg_send![&list, objectAtIndex: i];
                s.map(|s| s.to_string())
            })
            .collect()
    }
}

/// The system's preferred languages, from Windows' display language list.
#[cfg(windows)]
pub fn os_languages() -> Vec<String> {
    use windows_sys::Win32::Globalization::{GetUserPreferredUILanguages, MUI_LANGUAGE_NAME};
    let (mut count, mut len) = (0u32, 0u32);
    // SAFETY: the first call sizes the buffer, the second fills it; both
    // write only through the pointers given.
    unsafe {
        if GetUserPreferredUILanguages(MUI_LANGUAGE_NAME, &mut count, std::ptr::null_mut(), &mut len) == 0 || len == 0 {
            return Vec::new();
        }
        let mut buf = vec![0u16; len as usize];
        if GetUserPreferredUILanguages(MUI_LANGUAGE_NAME, &mut count, buf.as_mut_ptr(), &mut len) == 0 {
            return Vec::new();
        }
        buf.split(|&c| c == 0).filter(|s| !s.is_empty()).map(String::from_utf16_lossy).collect()
    }
}

/// The system's preferred languages, from the locale variables, as gettext
/// reads them: LANGUAGE's list first, then LC_ALL, LC_MESSAGES and LANG.
#[cfg(not(any(target_os = "macos", windows)))]
pub fn os_languages() -> Vec<String> {
    from_env(|k| std::env::var(k).ok())
}

#[cfg_attr(any(target_os = "macos", windows), allow(dead_code))]
fn from_env(var: impl Fn(&str) -> Option<String>) -> Vec<String> {
    let mut out = Vec::new();
    let mut add = |raw: &str| {
        // zh_CN.UTF-8@pinyin → zh-CN; C and POSIX name no language.
        let tag = raw.split(['.', '@']).next().unwrap_or("").replace('_', "-");
        if !tag.is_empty() && tag != "C" && tag != "POSIX" && !out.contains(&tag) {
            out.push(tag);
        }
    };
    if let Some(list) = var("LANGUAGE") {
        list.split(':').for_each(&mut add);
    }
    if let Some(one) = ["LC_ALL", "LC_MESSAGES", "LANG"].iter().find_map(|k| var(k).filter(|v| !v.is_empty())) {
        add(&one);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tags_map_to_the_languages_kiri_has() {
        for (tag, want) in [("zh", Some("zh-Hans")), ("zh-CN", Some("zh-Hans")), ("zh-Hans-SG", Some("zh-Hans")), ("zh_TW", Some("zh-Hans")), ("en-GB", Some("en")), ("fr-FR", None), ("", None)] {
            assert_eq!(lang_for(tag), want, "{tag}");
        }
        assert_eq!(resolve(Some("zh-Hans")), "zh-Hans");
        assert_eq!(resolve(Some("en")), "en");
        assert_eq!(resolve(Some("klingon")), "en");
    }

    #[test]
    fn locale_variables_read_like_gettext() {
        let env = |pairs: &'static [(&'static str, &'static str)]| move |k: &str| pairs.iter().find(|(n, _)| *n == k).map(|(_, v)| (*v).to_owned());
        assert_eq!(from_env(env(&[("LANG", "zh_CN.UTF-8")])), ["zh-CN"]);
        assert_eq!(from_env(env(&[("LANGUAGE", "fr:zh_CN"), ("LANG", "en_US.UTF-8")])), ["fr", "zh-CN", "en-US"]);
        assert_eq!(from_env(env(&[("LC_ALL", "C"), ("LANG", "de_DE")])), Vec::<String>::new());
        assert_eq!(from_env(env(&[])), Vec::<String>::new());
    }

    #[test]
    fn files_parse_and_the_native_words_translate() {
        for (tag, json) in FILES {
            let m: HashMap<String, String> = serde_json::from_str(json).unwrap_or_else(|e| panic!("lang/{tag}.json: {e}"));
            assert!(!m.is_empty(), "lang/{tag}.json is empty");
        }
        assert_eq!(tr("zh-Hans", "Settings…"), "设置…");
        assert_eq!(tr("en", "Settings…"), "Settings…");
        assert_eq!(tr("zh-Hans", "no such words"), "no such words");
        assert!(strings_js().starts_with("{\"zh-Hans\":{"));
        assert_eq!(fill("Started {name} on {ports}".into(), &[("name", &"web"), ("ports", &"8080")]), "Started web on 8080");
        assert_eq!(fill("{n} of {n}".into(), &[("n", &2)]), "2 of 2");
        assert_eq!(fill("Couldn't stop {name}: {error}".into(), &[("name", &"{error}"), ("error", &"no {such} thing")]), "Couldn't stop {error}: no {such} thing");
        assert_eq!(fill("{missing} and {} and {".into(), &[("n", &1)]), "{missing} and {} and {");
        assert_eq!(fill("宽 {w}，高 {h}".into(), &[("w", &"中"), ("h", &2)]), "宽 中，高 2");
    }
}

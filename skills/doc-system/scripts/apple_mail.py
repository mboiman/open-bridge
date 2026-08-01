#!/usr/bin/env python3
"""
Apple Mail source for doc-system.
Generic — no PII. Extract path comes from
workflow/contexts/doc-system.yaml sources[kind=apple_mail_flagged]
via env var DOC_SYSTEM_APPLE_MAIL_EXTRACT or --extract-to flag.

Usage:
    python3 apple_mail.py --list                       # geflaggte Mails listen
    python3 apple_mail.py --extract "Betreff"          # PDF extrahieren
    python3 apple_mail.py --unflag "Betreff"           # Flagge entfernen
    python3 apple_mail.py --move SRC DST               # Datei verschieben
    python3 apple_mail.py --cleanup                    # leeres Temp entfernen
"""

import subprocess
import argparse
import shutil
import os

DEFAULT_TEMP_DIR = os.environ.get(
    "DOC_SYSTEM_APPLE_MAIL_EXTRACT",
    os.path.expanduser("~/Desktop/_Mail_Attachments"),
)
TEMP_DIR = DEFAULT_TEMP_DIR

def run_applescript(script: str) -> str:
    """AppleScript ausfuehren und Ergebnis zurueckgeben."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=120
    )
    return result.stdout.strip()

def list_flagged_mails():
    """Alle geflaggten Mails mit PDF-Anhaengen auflisten."""
    script = '''
    tell application "Mail"
        set results to ""
        set counter to 0
        set seenSubjects to {}
        repeat with anAccount in every account
            repeat with aMailbox in every mailbox of anAccount
                try
                    set flaggedMsgs to (every message of aMailbox whose flagged status is true)
                    repeat with aMessage in flaggedMsgs
                        set subj to subject of aMessage
                        if subj is not in seenSubjects then
                            set hasPDF to false
                            repeat with att in mail attachments of aMessage
                                if name of att ends with ".pdf" then set hasPDF to true
                            end repeat
                            set counter to counter + 1
                            set end of seenSubjects to subj
                            set pdfFlag to ""
                            if hasPDF then set pdfFlag to " [PDF]"
                            set results to results & counter & ". " & subj & pdfFlag & linefeed & "   Von: " & (sender of aMessage) & linefeed & linefeed
                        end if
                    end repeat
                end try
            end repeat
        end repeat
        return results
    end tell
    '''
    print(run_applescript(script))

def extract_pdf(subject_pattern: str):
    """PDF-Anhaenge aus Mail mit passendem Betreff extrahieren."""
    os.makedirs(TEMP_DIR, exist_ok=True)
    script = f'''
    tell application "Mail"
        set targetFolder to "{TEMP_DIR}/"
        repeat with anAccount in every account
            repeat with aMailbox in every mailbox of anAccount
                try
                    set flaggedMsgs to (every message of aMailbox whose flagged status is true)
                    repeat with aMessage in flaggedMsgs
                        if subject of aMessage contains "{subject_pattern}" then
                            repeat with att in mail attachments of aMessage
                                if name of att ends with ".pdf" then
                                    save att in POSIX file (targetFolder & name of att)
                                    return "Saved: " & name of att
                                end if
                            end repeat
                        end if
                    end repeat
                end try
            end repeat
        end repeat
        return "Not found"
    end tell
    '''
    print(run_applescript(script))
    print(f"Temp-Ordner: {TEMP_DIR}")

def unflag_mail(subject_pattern: str):
    """Flagge von Mail mit passendem Betreff entfernen."""
    script = f'''
    tell application "Mail"
        repeat with anAccount in every account
            repeat with aMailbox in every mailbox of anAccount
                try
                    set flaggedMsgs to (every message of aMailbox whose flagged status is true)
                    repeat with aMessage in flaggedMsgs
                        if subject of aMessage contains "{subject_pattern}" then
                            set flagged status of aMessage to false
                            return "Flagge entfernt: " & subject of aMessage
                        end if
                    end repeat
                end try
            end repeat
        end repeat
        return "Not found"
    end tell
    '''
    print(run_applescript(script))

def move_file(src: str, dst: str):
    """Datei verschieben (Leerzeichen-sicher)."""
    shutil.move(src, dst)
    print(f"Verschoben: {os.path.basename(dst)}")

def render_html_body(subject_pattern: str):
    """Render the HTML body of the first matching flagged mail to a PDF.

    For mails without PDF attachments (e.g. Apple invoices, some bank
    notifications) — exports the message body as HTML and converts to PDF
    via cupsfilter. Result lands in TEMP_DIR alongside extracted PDFs.

    File is named after the subject (sanitized), or use --move afterwards.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    # AppleScript: get full source (includes HTML body and headers) of first match
    # We use `source` (raw RFC-822) and then strip headers in Python; or use
    # `content` (plain-text). For HTML body specifically there's no first-class
    # AppleScript hook — we get the raw source and extract text/html part.
    script = f'''
    tell application "Mail"
        repeat with anAccount in every account
            repeat with aMailbox in every mailbox of anAccount
                try
                    set flaggedMsgs to (every message of aMailbox whose flagged status is true)
                    repeat with aMessage in flaggedMsgs
                        if subject of aMessage contains "{subject_pattern}" then
                            set rawSource to source of aMessage
                            set msgSubject to subject of aMessage
                            return msgSubject & "::SPLIT::" & rawSource
                        end if
                    end repeat
                end try
            end repeat
        end repeat
        return "::NOTFOUND::"
    end tell
    '''
    out = run_applescript(script)
    if "::NOTFOUND::" in out or "::SPLIT::" not in out:
        print("Not found")
        return

    subject, raw = out.split("::SPLIT::", 1)
    # Extract HTML body from RFC-822 source — find first text/html part
    html = _extract_html_from_rfc822(raw)
    if not html:
        # Fallback: get plain `content` (text-only)
        script2 = f'''
        tell application "Mail"
            repeat with anAccount in every account
                repeat with aMailbox in every mailbox of anAccount
                    try
                        set flaggedMsgs to (every message of aMailbox whose flagged status is true)
                        repeat with aMessage in flaggedMsgs
                            if subject of aMessage contains "{subject_pattern}" then
                                return content of aMessage
                            end if
                        end repeat
                    end try
                end repeat
            end repeat
        end tell
        '''
        plain = run_applescript(script2)
        if not plain:
            print("Cannot extract body (no HTML part, no plain content).")
            return
        html = f"<html><head><meta charset='utf-8'><title>{_html_escape(subject)}</title></head><body><pre style='font-family:-apple-system,sans-serif;white-space:pre-wrap;'>{_html_escape(plain)}</pre></body></html>"

    # Sanitize subject to safe filename stem
    safe_stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in subject.strip())[:80]
    html_path = os.path.join(TEMP_DIR, f"{safe_stem}.html")
    pdf_path = os.path.join(TEMP_DIR, f"{safe_stem}.pdf")
    # Force UTF-8 meta charset so weasyprint/Chrome interpret correctly even if
    # the original HTML declared a wrong/missing charset.
    html = _force_utf8_meta(html)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # HTML -> PDF
    # macOS Tahoe removed the cupsfilter html->pdf chain. Try weasyprint
    # first (pure Python, deterministic), fall back to Chrome headless.
    if _html_to_pdf(html_path, pdf_path):
        try:
            os.remove(html_path)
        except OSError:
            pass
        print(f"Saved: {pdf_path}")
    else:
        print(f"HTML->PDF failed. HTML kept at: {html_path}")


def _html_to_pdf(html_path: str, pdf_path: str) -> bool:
    """Convert HTML file to PDF. Try weasyprint, then Chrome headless. Return True on success."""
    # 1. weasyprint
    try:
        import weasyprint  # type: ignore
        weasyprint.HTML(filename=html_path).write_pdf(pdf_path)
        return True
    except Exception as e:
        print(f"weasyprint failed: {e}")

    # 2. Chrome headless
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not os.path.exists(chrome):
        return False
    try:
        result = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={pdf_path}", f"file://{html_path}"],
            capture_output=True, timeout=60,
        )
        if result.returncode == 0 and os.path.exists(pdf_path):
            return True
        print(f"Chrome headless failed: {result.stderr.decode('utf-8', 'replace')[:400]}")
    except Exception as e:
        print(f"Chrome headless exception: {e}")
    return False


def _extract_html_from_rfc822(raw: str) -> str:
    """Return the body of the first text/html MIME part, decoded."""
    import email
    import email.policy
    msg = email.message_from_string(raw, policy=email.policy.default)
    html_part = None
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html_part = part
                break
    elif msg.get_content_type() == "text/html":
        html_part = msg
    if html_part is None:
        return ""
    try:
        return html_part.get_content()
    except Exception:
        # Fallback: bytes -> str
        payload = html_part.get_payload(decode=True)
        if not isinstance(payload, (bytes, bytearray)):
            return ""
        charset = html_part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def _force_utf8_meta(html: str) -> str:
    """Ensure the HTML declares UTF-8. Replace any existing meta charset,
    inject one into <head> if missing entirely.

    Many vendor invoice emails (notably Apple) come through with mojibake
    when the meta charset disagrees with the actual byte encoding. Python's
    email module decodes to a Python str (already correctly decoded), but
    when we write that str as UTF-8 bytes and the HTML still claims another
    charset, weasyprint/Chrome trust the meta and re-misinterpret.
    """
    import re
    # Drop any existing <meta charset=...> and <meta http-equiv="Content-Type" ...>
    html = re.sub(r'<meta[^>]*charset=[^>]*>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<meta[^>]*http-equiv=["\']?Content-Type["\']?[^>]*>', '', html, flags=re.IGNORECASE)
    utf8_meta = '<meta charset="utf-8">'
    if re.search(r'<head[^>]*>', html, flags=re.IGNORECASE):
        html = re.sub(r'(<head[^>]*>)', r'\1' + utf8_meta, html, count=1, flags=re.IGNORECASE)
    elif re.search(r'<html[^>]*>', html, flags=re.IGNORECASE):
        html = re.sub(r'(<html[^>]*>)', r'\1<head>' + utf8_meta + '</head>', html, count=1, flags=re.IGNORECASE)
    else:
        html = f'<html><head>{utf8_meta}</head><body>{html}</body></html>'
    return html


def cleanup_temp():
    """Temp-Ordner aufraeumen wenn leer."""
    if os.path.isdir(TEMP_DIR) and not os.listdir(TEMP_DIR):
        os.rmdir(TEMP_DIR)
        print("Temp-Ordner entfernt")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mail Attachment Processor")
    parser.add_argument("--list", action="store_true", help="Geflaggte Mails auflisten")
    parser.add_argument("--extract", type=str, help="PDF aus Mail extrahieren (Betreff-Pattern)")
    parser.add_argument("--unflag", type=str, help="Flagge entfernen (Betreff-Pattern)")
    parser.add_argument("--move", nargs=2, metavar=("SRC", "DST"), help="Datei verschieben")
    parser.add_argument("--cleanup", action="store_true", help="Temp-Ordner aufraeumen")
    parser.add_argument("--render-html", type=str, dest="render_html",
                        help="Body der Mail (HTML-Teil) als PDF rendern (Betreff-Pattern) — fuer Mails ohne PDF-Anhang (Apple-Rechnungen, Bank-Notifications)")
    parser.add_argument("--extract-to", type=str, default=None,
                        help="Override TEMP_DIR (default: env DOC_SYSTEM_APPLE_MAIL_EXTRACT or ~/Desktop/_Mail_Attachments)")

    args = parser.parse_args()
    if args.extract_to:
        TEMP_DIR = os.path.expanduser(args.extract_to)
        # propagate to module-level (functions read it via name)
        globals()["TEMP_DIR"] = TEMP_DIR

    if args.list:
        list_flagged_mails()
    elif args.extract:
        extract_pdf(args.extract)
    elif args.unflag:
        unflag_mail(args.unflag)
    elif args.move:
        move_file(args.move[0], args.move[1])
    elif args.cleanup:
        cleanup_temp()
    elif args.render_html:
        render_html_body(args.render_html)
    else:
        parser.print_help()

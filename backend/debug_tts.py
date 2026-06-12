"""
Diagnostic script: opens the Zalo TTS page, fills the textarea, clicks
whatever button is available, then dumps the full page HTML and all
interactive elements so we can find the correct selectors.

Run from backend folder:
    python debug_tts.py
"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

TTS_URL = "https://ai.zalo.solutions/products/text-to-audio-converter"
TEXTAREA_SELECTOR = "textarea[placeholder='Enter your content here']"
SAMPLE_TEXT = "Xin chào, đây là bài kiểm tra."


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)  # headful so we can watch
        page = browser.new_page()

        print("→ Navigating...")
        page.goto(TTS_URL, wait_until="networkidle", timeout=30_000)

        # Dismiss consent if present
        try:
            btn = page.locator("button:has-text('UNDERSTAND')")
            btn.wait_for(state="visible", timeout=5_000)
            btn.click()
            print("✓ Consent dismissed")
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            print("  (no consent dialog)")

        # Dump all buttons
        print("\n--- BUTTONS ON PAGE ---")
        buttons = page.locator("button").all()
        for i, b in enumerate(buttons):
            try:
                print(f"  [{i}] text={b.inner_text()!r:40s}  class={b.get_attribute('class')!r}")
            except Exception as e:
                print(f"  [{i}] error: {e}")

        # Fill textarea
        try:
            page.wait_for_selector(TEXTAREA_SELECTOR, timeout=10_000)
            page.fill(TEXTAREA_SELECTOR, SAMPLE_TEXT)
            print(f"\n✓ Textarea filled with: {SAMPLE_TEXT!r}")
        except Exception as e:
            print(f"\n✗ Could not fill textarea: {e}")
            # Try to find any textarea
            textareas = page.locator("textarea").all()
            for i, ta in enumerate(textareas):
                print(f"  textarea[{i}] placeholder={ta.get_attribute('placeholder')!r}  class={ta.get_attribute('class')!r}")

        # Click first visible button that could be "generate"
        print("\n--- BUTTONS AFTER FILL ---")
        buttons = page.locator("button").all()
        for i, b in enumerate(buttons):
            try:
                print(f"  [{i}] text={b.inner_text()!r:40s}  visible={b.is_visible()}  enabled={b.is_enabled()}")
            except Exception as e:
                print(f"  [{i}] error: {e}")

        # Try clicking every button and wait for audio
        clicked = False
        for i, b in enumerate(buttons):
            try:
                text = b.inner_text().strip()
                if b.is_visible() and b.is_enabled() and text not in ("UNDERSTAND", "SIGN IN", ""):
                    print(f"\n→ Clicking button [{i}]: {text!r}")
                    b.click()
                    clicked = True
                    break
            except Exception:
                pass

        if clicked:
            print("→ Waiting 15s for audio element...")
            time.sleep(15)

            # Check for audio element
            print("\n--- AUDIO ELEMENTS ---")
            audios = page.locator("audio").all()
            for i, a in enumerate(audios):
                print(f"  audio[{i}] src={a.get_attribute('src')!r}")
                sources = a.locator("source").all()
                for s in sources:
                    print(f"    source src={s.get_attribute('src')!r}")

            # Save full HTML snapshot
            html = page.content()
            Path("debug_page.html").write_text(html, encoding="utf-8")
            print("\n✓ Full HTML saved to debug_page.html")

            # Also show all elements with src containing audio/mp3/blob
            print("\n--- ELEMENTS WITH AUDIO SRC ---")
            for tag in ["audio", "source", "a"]:
                elems = page.locator(tag).all()
                for e in elems:
                    src = e.get_attribute("src") or e.get_attribute("href") or ""
                    if any(x in src for x in ["mp3", "audio", "blob", ".wav", "tts"]):
                        print(f"  <{tag}> src={src!r}")

        input("\nPress Enter to close browser...")
        browser.close()


if __name__ == "__main__":
    main()

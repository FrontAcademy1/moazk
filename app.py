import os, json, re, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, render_template, request, jsonify
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
SHOTS = ROOT / "screenshots"
REPORTS.mkdir(exist_ok=True)
SHOTS.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

ALLOWED_SCHEMES = {"http", "https"}

def valid_url(value):
    parsed = urlparse((value or "").strip())
    return parsed.scheme in ALLOWED_SCHEMES and bool(parsed.netloc)

def safe_filename(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:80] or "page"

def inspect_page(url):
    # Restrict obvious local/private-network targets to reduce SSRF risk.
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    blocked = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if host in blocked or host.endswith(".local"):
        raise ValueError("فحص عناوين localhost والشبكات الداخلية غير مسموح من هذه الواجهة.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    page_title, page_text, image_name = "", "", ""
    console_errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1365, "height": 900})
        page.on("pageerror", lambda exc: console_errors.append(str(exc)[:500]))
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(1200)
            page_title = page.title()
            page_text = page.locator("body").inner_text(timeout=5000)[:18000]
            image_name = safe_filename(stamp + "_" + (urlparse(url).hostname or "page")) + ".png"
            page.screenshot(path=str(SHOTS / image_name), full_page=True)
            buttons = page.get_by_role("button").all_text_contents()[:60]
            links = page.locator("a").all_text_contents()[:60]
            inputs = page.locator("input").evaluate_all(
                "(els) => els.slice(0,60).map(e => ({type:e.type, name:e.name, placeholder:e.placeholder, ariaLabel:e.getAttribute('aria-label')}))"
            )
            result = {
                "url": url, "title": page_title, "http_status": response.status if response else None,
                "text": page_text, "buttons": buttons, "links": links, "inputs": inputs,
                "page_errors": console_errors, "screenshot": "screenshots/" + image_name,
                "note": "الفحص يقرأ الواجهة فقط ولا يرسل إجابات أو يضغط أزرار الإرسال."
            }
        finally:
            browser.close()
    report_name = safe_filename(stamp + "_inspection.json")
    (REPORTS / report_name).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["report"] = "reports/" + report_name
    return result

@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/inspect")
def api_inspect():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not valid_url(url):
        return jsonify({"error": "أدخل رابطًا صحيحًا يبدأ بـ https:// أو http://"}), 400
    try:
        return jsonify(inspect_page(url))
    except Exception as exc:
        return jsonify({"error": str(exc)[:1000]}), 500

@app.post("/api/explain")
def api_explain():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()[:8000]
    if not question:
        return jsonify({"error": "اكتب نص السؤال أو محتوى الدرس أولًا."}), 400
    api_url = os.getenv("AI_API_URL", "").strip()
    api_key = os.getenv("AI_API_KEY", "").strip()
    model = os.getenv("AI_MODEL", "").strip()
    if not (api_url and api_key and model):
        return jsonify({
            "mode": "offline",
            "explanation": "لم يتم ضبط مزود AI بعد. احفظ AI_API_URL وAI_API_KEY وAI_MODEL في ملف .env ثم أعد تشغيل التطبيق. إلى ذلك الحين، استخدم وضع الدراسة: حدّد المطلوب، واكتب المعطيات، وحاول الحل قبل مراجعة الشرح.",
            "hint": "قسّم السؤال إلى: المطلوب، المعلومات المعطاة، القاعدة أو المفهوم المناسب، ثم تحقق من النتيجة."
        })
    # OpenAI-compatible chat-completions endpoint
    import urllib.request
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "أنت مدرس مساعد. ساعد الطالب على فهم السؤال بالتدرج، وقدم تلميحات وشرحًا تعليميًا بالعربية. لا تنفذ الاختبارات نيابة عن الطالب ولا ترسل إجابات إلى منصة تعليمية. إذا بدا أنه اختبار مُقيّم جارٍ، ركّز على المفهوم والخطوات العامة بدل إعطاء إجابة جاهزة."},
            {"role": "user", "content": "اشرح السؤال التالي تعليميًا مع تلميح وخطوات التفكير:\n" + question}
        ],
        "temperature": 0.3
    }
    req = urllib.request.Request(
        api_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type":"application/json", "Authorization":"Bearer " + api_key},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        answer = body["choices"][0]["message"]["content"]
        return jsonify({"mode":"ai", "explanation": answer})
    except Exception as exc:
        return jsonify({"error": "تعذر الاتصال بمزود AI. راجع إعدادات API والاتصال: " + str(exc)[:300]}), 502

@app.get("/health")
def health():
    return jsonify({"status":"ok", "browser":"Playwright/Chromium", "ai_configured": bool(os.getenv("AI_API_URL") and os.getenv("AI_API_KEY") and os.getenv("AI_MODEL"))})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)

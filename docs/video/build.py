"""Builds the Sentinel explainer video.

  python -I build.py            (run from this folder; needs edge-tts, ffmpeg, node, Chrome)

Each narration line gets its own voice clip (edge-tts, en-IN-NeerjaNeural) and its own
slide image (HTML rendered by headless Chrome) with the line as an on-screen subtitle.
Slides reveal their points as they are spoken. ffmpeg joins it all.
"""
import html
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / "work"
OUT = HERE.parent / "Sentinel-Explainer.mp4"
VOICE, RATE = "en-IN-NeerjaNeural", "-6%"
GAP, SCENE_GAP = 0.45, 0.55
FPS = 30

# --------------------------------------------------------------------- script
# Each scene: kind, data, lines. A line is (state, spoken text, subtitle text or None = same).
SCENES = [
    ("title", {"title": "Sentinel", "sub": "What our team built for Resurrect 2026, explained simply"}, [
        (0, "Hi team! In this video, I'll explain what we built for the Resurrect 2026 hackathon, in very simple words.", None),
        (0, "It's called Sentinel. A sentinel is a guard who keeps watch. And that's exactly what our app does for a factory.", None),
    ]),
    ("bullets", {"head": "The hackathon", "items": [
        "6 problems to choose from",
        "One rule: prove it works, with measured numbers",
        "We chose Problem 2: Smart Inventory Intelligence"]}, [
        (1, "The hackathon gave us six problems to choose from.", "The hackathon gave us 6 problems to choose from."),
        (2, "And it has one very important rule. Every team must prove its solution works, with measured numbers. If you can't measure it, you get no score.", None),
        (3, "So we picked Problem 2, Smart Inventory. It's the one we can measure exactly, and I'll show you how.", None),
    ]),
    ("story", {"head": "The problem, as a story"}, [
        (1, "Imagine a factory that makes mixer grinders, fans and heaters.", None),
        (2, "A shop orders thirteen mixer grinders, and the factory promises to deliver them by day twenty-one. That promised day is called the customer date.",
            "A shop orders 13 mixer grinders. The factory promises: by day 21. That's the customer date."),
        (3, "To build them, the factory needs parts, like switches. Some are in the store room. The rest are coming from a supplier.", None),
        (4, "Now the supplier becomes late. The switches that should come on day nineteen will now come on day twenty-six.",
            "The supplier becomes late. Switches due on day 19 will now come on day 26."),
        (5, "So the mixer grinders ship on day twenty-eight. Seven days late. And seven orders were waiting for the same switches.",
            "So they ship on day 28: 7 days late. And 7 orders were waiting for the same switches."),
        (6, "In most factories, nobody notices until day twenty-one, when the order fails to go out. By then, it's too late.",
            "In most factories, nobody notices until day 21. By then, it's too late."),
        (7, "If someone had noticed a week earlier, they could have rush-ordered the switches, and every order would ship on time. Noticing early is everything.", None),
    ]),
    ("bullets", {"head": "Three simple ideas", "items": [
        "Purchase order (PO) = our order to a supplier",
        "On hand = in the store room  ·  Incoming = on the way  ·  Allocated = promised to orders",
        "On hand + Incoming − Allocated = what is really free"]}, [
        (0, "Before we go on, three simple ideas.", None),
        (1, "One. A purchase order is just our order to a supplier, like an online order for parts.", None),
        (2, "Two. On hand means parts already in our store room. Incoming means parts on the way. Allocated means parts already promised to customer orders.", None),
        (3, "Three. On hand, plus incoming, minus allocated, tells us what is really free. We call that net availability.", None),
    ]),
    ("parts", {"head": "What we built: five parts"}, [
        (0, "Sentinel has five parts. Think of it like a person.", None),
        (1, "The ears. Every time something happens in the factory, a small message arrives. Sentinel ignores repeated messages, sets broken ones aside, and puts everything in time order. So messy data never changes a number.", None),
        (2, "The memory. It keeps an exact count of every material: on hand, incoming, allocated, and free.", None),
        (3, "The eyes. For every single order, it works out the day that order will really ship. It even learns which suppliers are usually late. If an order will ship after its customer date, it turns red.", None),
        (4, "The helper. This is the AI agent. It looks for a fix, like a rush order, and tests it first. It only suggests a fix that really helps.", None),
        (5, "And the human. Nothing is bought, and no email is sent, until a person clicks approve.", None),
    ]),
    ("bullets", {"head": "A pretend factory, with an answer key", "items": [
        "8 products · 24 materials · 6 suppliers · new orders every day",
        "Made messy on purpose: late suppliers, order rushes, missing stock, broken messages",
        "It knows the true answer for every order. Sentinel never sees it."]}, [
        (1, "We don't have a real factory, so we built a pretend one inside the computer. It has eight products, twenty-four materials and six suppliers.",
            "We built a pretend factory inside the computer: 8 products, 24 materials, 6 suppliers."),
        (2, "We made it messy on purpose: late suppliers, sudden rushes of orders, stock that goes missing, and broken messages.", None),
        (3, "And it knows the true answer for every order, like the answer key in an exam. Sentinel never sees the answer key. That's how we check if Sentinel is right.", None),
    ]),
    ("screen", {"head": "Day 4: everything is calm", "img": "calm"}, [
        (0, "Now let's look at the website. It's live at chatu dot in. Here it is on day four. Everything is calm, and every order is on track.",
            "Now the website, live at chatu.in. Day 4: everything is calm, every order on track."),
    ]),
    ("screen", {"head": "Day 14: a supplier slips a week", "img": "late", "hl": [4.5, 9, 46, 16]}, [
        (0, "On day fourteen, we simulate a problem. A supplier pushes a delivery back by a week.", "Day 14: we simulate a problem. A supplier pushes a delivery back a week."),
        (1, "Sentinel reacts straight away. The headline says seven orders will miss their customer date, and it tells us why: they're all waiting for the same switches.", None),
    ]),
    ("screen", {"head": "The order runway", "img": "late", "hl": [4.2, 28, 53.5, 35], "always": True}, [
        (0, "This is the order runway. Each bar runs from today to the day the order will really ship.", None),
        (0, "The small green tick is the customer date. If the bar goes past it, that part turns red. That's how late it will be.", None),
    ]),
    ("screen", {"head": "Click an order: exactly why", "img": "explain"}, [
        (0, "Click any order, and Sentinel shows exactly where each part is coming from, and which one is holding it up.", None),
    ]),
    ("screen", {"head": "The fix, tested before it's shown", "img": "decision"}, [
        (0, "Here's the helper's suggestion: rush-order ninety-five switches. It costs about three thousand eight hundred and fifty rupees, and late orders go from seven to zero.",
            "The suggestion: rush-order 95 switches. Cost about ₹3,850. Late orders: 7 → 0."),
    ]),
    ("screen", {"head": "Only planners and buyers can buy", "img": "manager"}, [
        (0, "A manager can see this, but cannot approve it. The button is locked. Only planners and buyers can buy.", None),
    ]),
    ("screen", {"head": "A person approves", "img": "approved", "hl": [4.5, 9, 54, 16]}, [
        (0, "The planner clicks approve. A new purchase order is created, and late orders drop to zero.", None),
    ]),
    ("screen", {"head": "Vague supplier messages (Topic 6)", "img": "vague"}, [
        (0, "Suppliers rarely send a clean date. Here one writes: postponed to next Wednesday. Is that this coming Wednesday, or the one after? A normal system picks one date and hopes.",
            "\u201cPostponed to next Wednesday\u201d: this coming one, or the one after? A normal system picks one and hopes."),
        (0, "Sentinel shows every day the message could mean, plans for the latest one, so nothing is late by surprise, and drafts one question for the supplier. It is sent only when a planner approves.",
            "Sentinel shows what it could mean, plans for the latest day, and drafts one question, sent only after approval."),
        (0, "This is how we link Topic six, ambiguity, into our inventory app.", "This links Topic 6, ambiguity, into our inventory app."),
    ]),
    ("screen", {"head": "It warns when data stops", "img": "outage", "hl": [4.5, 9, 40, 16]}, [
        (0, "Sentinel also warns when a data feed goes quiet, instead of quietly showing old numbers.", None),
    ]),
    ("phones", {"head": "Works on a phone too", "imgs": ["phone-today", "phone-orders"]}, [
        (0, "And it works nicely on a phone, with four simple tabs at the bottom.", None),
    ]),
    ("bignum", [
        ("6,455 orders", "30 pretend factories · 60 days each · the same orders in every test"),
        ("0 mistakes", "in 129,600 stock figures, checked against a separate calculator"),
        ("73% right", "of warnings given a week ahead · a lazy rule is right only 20%"),
        ("93.4% → 96.7%", "orders delivered on time · better in 26 of 30 factories"),
        ("+0.75% spend", "simple rules that do as well spend 1.5% to 9% more"),
        ("135 of 150", "vague supplier messages read right · written blind by a separate AI"),
    ], [
        (0, "Now the most important part: the proof. We tested Sentinel on thirty pretend factories, sixty days each. That's six thousand four hundred and fifty-five orders, and every test uses exactly the same orders.",
            "The proof: 30 pretend factories, 60 days each, 6,455 orders. The same orders in every test."),
        (1, "First, are the numbers right? We checked one lakh twenty-nine thousand six hundred stock figures against a separate calculator. Zero mistakes.",
            "Are the numbers right? 129,600 stock figures checked. 0 mistakes."),
        (2, "Second, does it warn early? A week before the customer date, seventy-three percent of its warnings are right. A lazy rule that warns about every order not yet started is right only twenty percent of the time.",
            "A week ahead, 73% of its warnings are right. A lazy rule: only 20%."),
        (3, "Third, does the factory do better? Orders delivered on time went up from ninety-three point four percent to ninety-six point seven percent. Better in twenty-six of thirty factories.",
            "On-time delivery: 93.4% → 96.7%. Better in 26 of 30 factories."),
        (4, "It does spend a little more, about three quarters of a percent, because rush orders cost extra. Simple rules that reach the same on-time level spend between one and a half and nine percent more.",
            "It spends 0.75% more. Simple rules that do as well spend 1.5% to 9% more."),
        (5, "And for Topic six: on one hundred and fifty supplier messages written by a separate AI that never saw our code, Sentinel read one hundred and thirty-five correctly on its first try.",
            "Topic 6: 135 of 150 blind test messages read right, first try."),
    ]),
    ("bullets", {"head": "Being honest about the limits", "items": [
        "It's a pretend factory. No real planner has used it yet.",
        "About 1 in 4 warnings is a false alarm.",
        "Until a supplier answers, its planned date is often later than the real one."]}, [
        (0, "We are also honest about the limits. Judges respect that.", None),
        (1, "It's a pretend factory, and no real planner has used it yet.", None),
        (2, "About one in four warnings turns out to be a false alarm. We chose that on purpose, because missing a real late order is much worse.", None),
        (3, "And for vague messages, until the supplier answers, Sentinel plans a little too late rather than too early. That is on purpose too.", None),
    ]),
    ("bullets", {"head": "What we need from you", "items": [
        "Open chatu.in on your phone and laptop. Play with it.",
        "Practise the 3-minute demo",
        "Learn the key numbers: 93.4% → 96.7% · +0.75% spend · 73% of week-ahead warnings right",
        "Before presenting: Simulate → Start over",
        "Read the team guide PDF for every detail"]}, [
        (0, "So, what do we need from you?", None),
        (1, "Open chatu dot in on your phone and laptop, and play with it.", "Open chatu.in on your phone and laptop, and play with it."),
        (2, "Practise the three-minute demo.", "Practise the 3-minute demo."),
        (3, "Learn the key numbers. Ninety-three point four to ninety-six point seven percent on time, for three quarters of a percent more spend, and seventy-three percent of warnings right a week ahead.",
            "Learn the key numbers: 93.4% → 96.7% on time, +0.75% spend, 73% of week-ahead warnings right."),
        (4, "Before presenting, press start over, so the demo begins from day four.", None),
        (5, "And read the team guide PDF for all the details.", None),
    ]),
    ("title", {"title": "Sentinel", "sub": "Spots late orders a week early · explains why · suggests a fix · a person decides", "url": "chatu.in"}, [
        (0, "That's Sentinel. It spots late orders a week early, tells you why, suggests a fix, and lets a person decide. Let's go win this!", None),
    ]),
]

# --------------------------------------------------------------------- slides
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
*{box-sizing:border-box} html,body{margin:0;width:1920px;height:1080px;overflow:hidden}
body{background:#f2efe8;color:#1c1d1b;font:34px/1.4 'IBM Plex Sans',sans-serif;position:relative}
.top{position:absolute;left:110px;right:110px;top:56px;display:flex;align-items:center;gap:18px;font-size:24px;color:#7b7a72}
.mark{width:30px;height:30px;border:3px solid #1c1d1b;border-radius:3px;position:relative}
.mark::after{content:'';position:absolute;left:5px;right:5px;top:10px;height:4px;background:#c23b12}
.top b{color:#1c1d1b;font-weight:600}
h1{font:500 68px/1.1 'Fraunces',serif;letter-spacing:-1px;margin:0}
.main{position:absolute;left:110px;right:110px;top:140px;bottom:190px}
.cap{position:absolute;left:0;right:0;bottom:0;min-height:150px;background:#1c1d1b;color:#f2efe8;padding:30px 110px;font-size:36px;line-height:1.35;display:flex;align-items:center}
ul.b{list-style:none;padding:0;margin:44px 0 0}
ul.b li{display:flex;gap:28px;align-items:baseline;padding:22px 0;border-top:2px solid #dcd7cb;font-size:42px;transition:none}
ul.b li:last-child{border-bottom:2px solid #dcd7cb}
ul.b li i{font:500 32px 'IBM Plex Mono',monospace;color:#c23b12;font-style:normal;min-width:44px}
.off{opacity:0}
.title{position:absolute;left:110px;right:110px;top:290px}
.title h1{font-size:150px;letter-spacing:-3px}
.title p{font-size:44px;color:#4a4a45;max-width:1500px;margin:22px 0 0}
.title .url{font:500 52px 'IBM Plex Mono',monospace;color:#c23b12;margin-top:40px}
.shot{position:absolute;left:0;right:0;top:100px;bottom:0;display:flex;justify-content:center;align-items:flex-start}
.frame{position:relative;border:2px solid #1c1d1b;border-radius:10px;overflow:hidden;box-shadow:0 18px 40px rgba(0,0,0,.12);background:#f2efe8}
.frame.full{width:1700px;height:640px}
.frame.full .wrap{position:relative;width:1700px}
.frame.full img{display:block;width:1700px}
.frame.crop .wrap{position:relative}
.frame.crop img{display:block;height:620px;max-width:1700px;object-fit:contain}
.hl{position:absolute;border:5px solid #c23b12;border-radius:12px}
.phones{display:flex;gap:60px;justify-content:center}
.phones img{height:660px;border:2px solid #1c1d1b;border-radius:26px}
.big{margin-top:70px}
.big .n{font:500 150px/1 'Fraunces',serif;letter-spacing:-3px}
.big .n em{font-style:normal;color:#3d6b4c}
.big .l{font-size:44px;color:#4a4a45;margin-top:28px;max-width:1500px}
.dots{display:flex;gap:14px;margin-top:60px}.dots span{width:16px;height:16px;border-radius:50%;background:#dcd7cb}.dots span.on{background:#1c1d1b}
.story{display:grid;grid-template-columns:repeat(4,1fr);gap:26px;margin-top:46px}
.card{border:2px solid #1c1d1b;border-radius:14px;padding:26px;background:#faf8f3;min-height:330px}
.card .d{font:500 30px 'IBM Plex Mono',monospace;color:#7b7a72}
.card .t{font:500 40px/1.2 'Fraunces',serif;margin:12px 0}
.card p{font-size:28px;margin:0;color:#4a4a45}
.card.red{border-color:#c23b12;background:#fbefe9}.card.red .t{color:#c23b12}
.note{margin-top:34px;font-size:38px;max-width:1650px}
.note b{color:#c23b12}.note .g{color:#3d6b4c;font-weight:600}
.parts{display:grid;grid-template-columns:repeat(5,1fr);gap:22px;margin-top:50px}
.part{border:2px solid #1c1d1b;border-radius:14px;padding:26px 22px;min-height:470px;background:#faf8f3}
.part .k{font:500 26px 'IBM Plex Mono',monospace;color:#c23b12}
.part .t{font:500 44px/1.15 'Fraunces',serif;margin:14px 0 16px}
.part p{font-size:27px;line-height:1.4;margin:0;color:#4a4a45}
.part.on{background:#1c1d1b;color:#f2efe8}.part.on p{color:#d9d6cc}.part.on .k{color:#f0784a}
"""


def page(body, cap):
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>"
            f"<div class='top'><div class='mark'></div><b>Sentinel</b><span>· explained for the team</span></div>"
            f"{body}<div class='cap'>{html.escape(cap)}</div></body></html>")


def slide(kind, data, state, cap):
    e = html.escape
    if kind == "title":
        url = f"<div class='url'>{e(data['url'])}</div>" if data.get("url") else ""
        return page(f"<div class='title'><h1>{e(data['title'])}</h1><p>{e(data['sub'])}</p>{url}</div>", cap)
    if kind == "bullets":
        lis = "".join(f"<li class='{'' if i < state else 'off'}'><i>{i + 1:02d}</i><span>{e(t)}</span></li>"
                      for i, t in enumerate(data["items"]))
        return page(f"<div class='main'><h1>{e(data['head'])}</h1><ul class='b'>{lis}</ul></div>", cap)
    if kind == "story":
        cards = [("", "Factory", "Makes mixer grinders, fans and heaters", ""),
                 ("order", "13 mixer grinders", "Promised by day 21: the customer date", ""),
                 ("parts", "Needs switches", "Some in the store room, the rest from a supplier", ""),
                 ("day 19 → 26", "Supplier is late", "Switches now come a week later", "red")]
        shown = min(state, 4)
        html_cards = "".join(
            f"<div class='card {c[3]} {'' if i < shown else 'off'}'><div class='d'>{e(c[0])}</div><div class='t'>{e(c[1])}</div><p>{e(c[2])}</p></div>"
            for i, c in enumerate(cards))
        notes = ["", "", "", "", "",
                 "Ships day 28: <b>7 days late</b>. And <b>6 orders</b> wait for the same switches.",
                 "Most factories notice on day 21. <b>Too late.</b>",
                 "Notice on day 14 → rush-order the switches → <span class='g'>everything ships on time.</span>"]
        note = "<br>".join(n for n in notes[5:state + 1] if n)
        return page(f"<div class='main'><h1>{e(data['head'])}</h1><div class='story'>{html_cards}</div>"
                    f"<div class='note'>{note}</div></div>", cap)
    if kind == "parts":
        parts = [("01", "The ears", "Reads every message. Ignores repeats, sets broken ones aside."),
                 ("02", "The memory", "Exact count of every material: on hand, incoming, allocated, free."),
                 ("03", "The eyes", "Works out the real ship day of every order. Late = red."),
                 ("04", "The helper", "AI agent. Finds a fix and tests it before suggesting it."),
                 ("05", "The human", "Nothing is bought until a person clicks approve.")]
        cells = "".join(
            f"<div class='part {'on' if i + 1 == state else ''} {'' if (state == 0 or i < state) else 'off'}'>"
            f"<div class='k'>{p[0]}</div><div class='t'>{e(p[1])}</div><p>{e(p[2])}</p></div>"
            for i, p in enumerate(parts))
        return page(f"<div class='main'><h1>{e(data['head'])}</h1><div class='parts'>{cells}</div></div>", cap)
    if kind == "screen":
        hl = ""
        if data.get("hl") and (state == 1 or data.get("always") or data["img"] in ("approved", "outage")):
            x, y, w, h = data["hl"]
            hl = f"<div class='hl' style='left:{x}%;top:{y}%;width:{w}%;height:{h}%'></div>"
        src = (HERE / "screens" / f"{data['img']}.png").as_uri()
        mode = "crop" if data["img"] in ("runway", "decision", "manager", "vague") else "full"
        return page(f"<div class='main'><h1>{e(data['head'])}</h1><div class='shot'><div class='frame {mode}'><div class='wrap'>"
                    f"<img src='{src}'>{hl}</div></div></div></div>", cap)
    if kind == "phones":
        imgs = "".join(f"<img src='{(HERE / 'screens' / f'{i}.png').as_uri()}'>" for i in data["imgs"])
        return page(f"<div class='main'><h1>{e(data['head'])}</h1><div class='shot'><div class='phones'>{imgs}</div></div></div>", cap)
    if kind == "bignum":
        n, l = data[state]
        n_html = e(n).replace("→ 96.7%", "→ <em>96.7%</em>")
        dots = "".join(f"<span class='{'on' if i == state else ''}'></span>" for i in range(len(data)))
        return page(f"<div class='main'><h1>The proof</h1><div class='big'><div class='n'>{n_html}</div>"
                    f"<div class='l'>{e(l)}</div></div><div class='dots'>{dots}</div></div>", cap)
    raise ValueError(kind)


# --------------------------------------------------------------------- helpers
def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"FAILED: {' '.join(map(str, cmd))}\n{r.stderr[-1500:]}")
    return r.stdout


def dur(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]))


def tts(i, text):
    mp3 = WORK / f"v{i:03d}.mp3"
    if not mp3.exists():
        run([sys.executable, "-m", "edge_tts", "--voice", VOICE, f"--rate={RATE}", "--text", text, "--write-media", str(mp3)])
    return mp3


def main():
    WORK.mkdir(exist_ok=True)
    segs = []
    for si, (kind, data, lines) in enumerate(SCENES):
        for li, (state, said, cap) in enumerate(lines):
            segs.append({"kind": kind, "data": data, "state": state, "said": said, "cap": cap or said,
                         "last": li == len(lines) - 1})
    print(f"{len(segs)} lines")
    with ThreadPoolExecutor(6) as ex:
        mp3s = list(ex.map(lambda a: tts(*a), [(i, s["said"]) for i, s in enumerate(segs)]))
    jobs = []
    for i, s in enumerate(segs):
        h = WORK / f"s{i:03d}.html"
        h.write_text(slide(s["kind"], s["data"], s["state"], s["cap"]), encoding="utf-8")
        jobs.append({"html": str(h), "png": str(WORK / f"s{i:03d}.png")})
    (WORK / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    print(run(["node", str(HERE / "render.mjs"), str(WORK / "jobs.json")]).strip())

    # audio: each clip padded to its slot; image list with the same slot lengths
    lead = 0.8
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", str(lead), str(WORK / "lead.wav")])
    alist, vlist, total = [f"file '{(WORK / 'lead.wav').as_posix()}'"], [], lead
    vlist.append(f"file '{Path(jobs[0]['png']).as_posix()}'\nduration {lead:.3f}")
    for i, (s, mp3) in enumerate(zip(segs, mp3s)):
        slot = round((dur(mp3) + GAP + (SCENE_GAP if s["last"] else 0)) * FPS) / FPS
        wav = WORK / f"a{i:03d}.wav"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(mp3), "-af", f"apad=whole_dur={slot}", "-ar", "48000", "-ac", "1", "-t", f"{slot}", str(wav)])
        alist.append(f"file '{wav.as_posix()}'")
        vlist.append(f"file '{Path(jobs[i]['png']).as_posix()}'\nduration {slot:.3f}")
        total += slot
    vlist.append(f"file '{Path(jobs[-1]['png']).as_posix()}'")
    (WORK / "a.txt").write_text("\n".join(alist), encoding="utf-8")
    (WORK / "v.txt").write_text("\n".join(vlist), encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(WORK / "a.txt"), "-c", "pcm_s16le", str(WORK / "voice.wav")])
    fade_out = total - 0.8
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(WORK / "v.txt"), "-i", str(WORK / "voice.wav"),
         "-vf", f"fps={FPS},format=yuv420p,fade=t=in:st=0:d=0.6,fade=t=out:st={fade_out:.2f}:d=0.8",
         "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=out:st={fade_out:.2f}:d=0.8",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-tune", "stillimage", "-c:a", "aac", "-b:a", "160k",
         "-ar", "48000", "-t", f"{total:.2f}", "-movflags", "+faststart", str(OUT)])
    print(f"done: {OUT} ({total / 60:.1f} min)")


if __name__ == "__main__":
    main()

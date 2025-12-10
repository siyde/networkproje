# server.py — Game Hub WS Sunucusu (Pictionary + TTT + Codenames + PixelWar)
import asyncio, json, secrets, random, re, os, math
from typing import Dict
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.add_middleware(CORSMMiddleware := CORSMiddleware,
                   allow_origins=["*"],
                   allow_headers=["*"],
                   allow_methods=["*"])

# ====== Pictionary ======
pictionary_rooms: Dict[str, dict] = {}

# ====== Codenames ======
codenames_rooms: Dict[str, dict] = {}

# ====== Tic Tac Toe ======
ttt_rooms: Dict[str, dict] = {}

# ====== Pixelwar ======
pixelwar_rooms: Dict[str, dict] = {}

# ====== Sumo Bash (yuvarlak arena mini game) ======
sumo_rooms: Dict[str, dict] = {}

# ==========================
# Pictionary (çok odalı)
# ==========================
ROUND_SECONDS = 75
INTERMISSION = 5
CHOICE_SECONDS = 10

PIC_WORDS = [
    "elma","araba","kedi","köpek","ev","güneş","ay","bulut","masa","kalem",
    "tren","uçak","ağaç","kalp","balık","pizza","robot","dağ","deniz","zil",
    "fener","kitap","okul","yıldız","kamera","kule","şehir","çiçek","gözlük","çorap"
]

# İstersen burada sabit toplam tur sayısı tanımlayabilirsin (sadece görsel amaçlı)
PIC_TOTAL_ROUNDS = 10


def mask_word(w):
    # Kelimeyi "_ _ _" formatına çevir
    return " ".join(["_" if ch != " " else " " for ch in w])

async def ws_send(ws, payload):
    await ws.send_text(json.dumps(payload))

async def pic_broadcast(room, payload):
    msg = json.dumps(payload)
    dead = []
    for ws in list(room["clients"]):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        room["clients"].discard(ws)
        pid = getattr(ws, "state_pid", None)
        if pid and pid in room["ws_by_pid"] and room["ws_by_pid"][pid] is ws:
            room["ws_by_pid"].pop(pid, None)

def pic_room(room_id):
    if room_id not in pic_rooms:
        pic_rooms[room_id] = {
            "clients": set(),
            "ws_by_pid": {},
            "players": {},          # pid -> {name, score}
            "drawer_order": [],
            "drawer_idx": 0,
            "current_drawer": None,
            "word": None,
            "choices": None,
            "chosen": False,
            "strokes": [],
            "started": False,
            "seconds_left": 0,
            "round_task": None,
            "password": None,
            "invite_key": None,
            # yeni alanlar
            "round_index": 0,
            "total_rounds": PIC_TOTAL_ROUNDS,
            "hint_mask": None,
            "hint_used": False,
        }
    return pic_rooms[room_id]

def pic_next_drawer(room):
    if not room["drawer_order"]:
        return None
    pid = room["drawer_order"][room["drawer_idx"] % len(room["drawer_order"])]
    room["drawer_idx"] = (room["drawer_idx"] + 1) % len(room["drawer_order"])
    return pid

async def pic_state_push(room_id):
    room = pic_rooms.get(room_id)
    if not room:
        return

    for ws in list(room["clients"]):
        pid = getattr(ws, "state_pid", None)

        # Kelime görünümü: çizen tam kelimeyi görür, diğerleri maske / ipucu maskesi
        if room["chosen"] and room["word"]:
            if pid == room.get("current_drawer"):
                word_view = room["word"]
            else:
                # ipucu maskesi varsa onu kullan, yoksa klasik mask_word
                if room.get("hint_mask"):
                    word_view = room["hint_mask"]
                else:
                    word_view = mask_word(room["word"])
        else:
            if pid == room.get("current_drawer"):
                word_view = "(kelime seçiliyor)"
            else:
                word_view = ""

        try:
            await ws.send_text(json.dumps({
                "type": "state",
                "players": room["players"],
                "drawer": room.get("current_drawer"),
                "word": word_view,
                "secondsLeft": room.get("seconds_left", 0),
                "strokes": room["strokes"],
                "started": room["started"],
                "round": room.get("round_index", 1),
                "totalRounds": room.get("total_rounds") or 0,
                "hintUsed": room.get("hint_used", False),
            }))
        except Exception:
            room["clients"].discard(ws)

async def pic_start_round(room_id):
    room = pic_rooms.get(room_id)
    if not room or len(room["players"]) < 2:
        room["started"] = False
        room["word"] = None
        room["strokes"].clear()
        room["seconds_left"] = 0
        room["choices"] = None
        room["chosen"] = False
        room["hint_mask"] = None
        room["hint_used"] = False
        await pic_broadcast(room, {"type": "info", "msg": "Yeni tur için en az 2 oyuncu gerekli."})
        await pic_state_push(room_id)
        return

    # Tur sayacını artır
    room["round_index"] = room.get("round_index", 0) + 1
    room["hint_used"] = False
    room["hint_mask"] = None

    drawer = pic_next_drawer(room)
    room["current_drawer"] = drawer
    room["strokes"].clear()
    room["started"] = True
    room["seconds_left"] = 0
    room["chosen"] = False
    room["word"] = None
    room["choices"] = random.sample(PIC_WORDS, 3)

    await pic_broadcast(room, {"type": "round_start", "drawer": drawer, "round": room["round_index"]})
    await pic_state_push(room_id)

    # Çizene kelime seçim penceresi
    ws = room["ws_by_pid"].get(drawer)
    if ws:
        await ws_send(ws, {"type": "choose_word", "choices": room["choices"], "timeout": CHOICE_SECONDS})

    # Kelime seçilmesi için süre
    try:
        for _ in range(CHOICE_SECONDS):
            await asyncio.sleep(1)
            if room["chosen"]:
                break
        if not room["chosen"] and room["choices"]:
            room["word"] = random.choice(room["choices"])
            room["chosen"] = True
            room["hint_mask"] = mask_word(room["word"])
            await pic_broadcast(room, {"type": "info", "msg": "Kelime otomatik seçildi."})
    except asyncio.CancelledError:
        return

    room["seconds_left"] = ROUND_SECONDS
    await pic_state_push(room_id)

    # Tur süresi geri sayımı
    try:
        while room["seconds_left"] > 0:
            await asyncio.sleep(1)
            room["seconds_left"] -= 1
            if room["seconds_left"] % 5 == 0 or room["seconds_left"] <= 5:
                await pic_state_push(room_id)
        await pic_broadcast(room, {"type": "round_end", "result": "timeup", "word": room["word"]})
    except asyncio.CancelledError:
        return

    await asyncio.sleep(INTERMISSION)
    room["round_task"] = asyncio.create_task(pic_start_round(room_id))

async def pic_end_round_with_winner(room_id, winner_pid):
    room = pic_rooms.get(room_id)
    if not room:
        return

    # Skor güncelleme
    if winner_pid in room["players"]:
        room["players"][winner_pid]["score"] += 10
    d = room.get("current_drawer")
    if d and d in room["players"]:
        room["players"][d]["score"] += 5

    winner_name = room["players"].get(winner_pid, {}).get("name", winner_pid)

    await pic_broadcast(room, {
        "type": "round_end",
        "result": "guessed",
        "winner": winner_pid,
        "winnerName": winner_name,
        "word": room["word"]
    })

    task = room.get("round_task")
    if task and not task.done():
        task.cancel()

    await asyncio.sleep(INTERMISSION)
    room["round_task"] = asyncio.create_task(pic_start_round(room_id))

@app.websocket("/ws/pictionary")
async def pictionary_ws(ws: WebSocket):
    await ws.accept()
    room_id = None
    pid = secrets.token_hex(3)
    ws.state_pid = pid
    try:
        while True:
            data = json.loads(await ws.receive_text())
            typ = data.get("type")

            if typ == "join":
                room_id = data["roomId"]
                name = data.get("name", "anon")[:24]
                provided_pwd = data.get("password")
                provided_key = data.get("inviteKey")
                mode = data.get("mode", "join")   # create / join

                if room_id not in pic_rooms and mode != "create":
                    await ws_send(ws, {"type": "join_error", "reason": "no_such_room"})
                    continue

                new_room = room_id not in pic_rooms
                room = pic_room(room_id)

                # Şifre kontrolü (varsayılan logic'i istersen buraya ekleyebiliriz;
                # şimdilik sadece odanın password alanını dolduruyoruz)
                room["clients"].add(ws)
                room["ws_by_pid"][pid] = ws
                room["players"][pid] = {"name": name, "score": 0}
                if pid not in room["drawer_order"]:
                    room["drawer_order"].append(pid)

                if new_room:
                    room["password"] = provided_pwd or None
                    room["invite_key"] = secrets.token_urlsafe(12)

                await ws_send(ws, {
                    "type": "joined",
                    "pid": pid,
                    "room": room_id,
                    "hasPassword": room["password"] is not None,
                    "inviteKey": room.get("invite_key")
                })

                await pic_broadcast(room, {
                    "type": "system",
                    "msg": f"{name} katıldı",
                    "players": room["players"]
                })

                if not room["started"] and len(room["players"]) >= 2:
                    room["round_task"] = asyncio.create_task(pic_start_round(room_id))
                else:
                    await pic_state_push(room_id)

            elif typ == "choose_word" and room_id:
                room = pic_rooms.get(room_id)
                if not room or room.get("current_drawer") != pid:
                    continue
                choice = str(data.get("choice", "")).strip()
                if room.get("choices") and choice in room["choices"]:
                    room["word"] = choice
                    room["chosen"] = True
                    room["hint_mask"] = mask_word(room["word"])
                    await pic_broadcast(room, {"type": "info", "msg": "Kelime seçildi!"})
                    await pic_state_push(room_id)

            elif typ == "leave" and room_id:
                break

            elif typ == "stroke" and room_id:
                room = pic_rooms.get(room_id)
                if not room or room.get("current_drawer") != pid or not room.get("chosen"):
                    continue
                s = {
                    "x0": data["x0"],
                    "y0": data["y0"],
                    "x1": data["x1"],
                    "y1": data["y1"],
                    "w": data.get("w", 2),
                    "c": data.get("c", "#000")
                }
                room["strokes"].append(s)
                await pic_broadcast(room, {"type": "stroke", "stroke": s})

            elif typ == "clear" and room_id:
                room = pic_rooms.get(room_id)
                if not room or room.get("current_drawer") != pid or not room.get("chosen"):
                    continue
                room["strokes"].clear()
                await pic_broadcast(room, {"type": "clear"})
                await pic_state_push(room_id)

            elif typ == "undo" and room_id:
                # Yeni: son çizgiyi geri al
                room = pic_rooms.get(room_id)
                if not room or room.get("current_drawer") != pid or not room.get("chosen"):
                    continue
                if room["strokes"]:
                    room["strokes"].pop()
                    await pic_state_push(room_id)

            elif typ == "hint" and room_id:
                # Yeni: ipucu sistemi (sadece çizen, tur başına 1 kez)
                room = pic_rooms.get(room_id)
                if not room or room.get("current_drawer") != pid:
                    continue
                if not room.get("chosen") or not room.get("word"):
                    continue
                if room.get("hint_used"):
                    continue

                w = room["word"]
                if not room.get("hint_mask"):
                    room["hint_mask"] = mask_word(w)

                # Henüz açılmamış bir harf pozisyonu bul
                candidates = [
                    i for i, ch in enumerate(w)
                    if ch != " " and room["hint_mask"][i] == "_"
                ]
                if not candidates:
                    room["hint_used"] = True
                    await ws_send(ws, {"type": "info", "msg": "Tüm harfler zaten açık, ipucu verilemedi."})
                    await pic_state_push(room_id)
                    continue

                idx = random.choice(candidates)
                hm = list(room["hint_mask"])
                hm[idx] = w[idx]
                room["hint_mask"] = "".join(hm)
                room["hint_used"] = True

                # Çizenden 3 puan sil
                if pid in room["players"]:
                    room["players"][pid]["score"] = max(0, room["players"][pid]["score"] - 3)

                await pic_broadcast(room, {
                    "type": "info",
                    "msg": "✏️ Çizen bir ipucu verdi! (3 puan kaybetti)"
                })
                await pic_state_push(room_id)

            elif typ == "chat" and room_id:
                room = pic_rooms.get(room_id)
                if not room:
                    continue
                text = str(data.get("text", ""))[:200]

                if room.get("word") and room.get("chosen") and text.strip():
                    norm = lambda s: re.sub(r"\s+", "", s.lower())
                    if norm(text) == norm(room["word"]):
                        await pic_broadcast(room, {
                            "type": "guess",
                            "pid": pid,
                            "name": room["players"][pid]["name"],
                            "correct": True
                        })
                        await pic_end_round_with_winner(room_id, pid)
                        continue

                await pic_broadcast(room, {
                    "type": "chat",
                    "pid": pid,
                    "name": room["players"][pid]["name"],
                    "text": text
                })

    except WebSocketDisconnect:
        pass
    finally:
        if room_id and room_id in pic_rooms:
            room = pic_rooms[room_id]
            info = room["players"].pop(pid, None)
            room["clients"].discard(ws)
            room["ws_by_pid"].pop(pid, None)
            if pid in room["drawer_order"]:
                room["drawer_order"].remove(pid)
            if not room["clients"]:
                t = room.get("round_task")
                if t and not t.done():
                    t.cancel()
                pic_rooms.pop(room_id, None)
            else:
                await pic_broadcast(room, {
                    "type": "system",
                    "msg": f"{(info or {}).get('name','?')} ayrıldı",
                    "players": room["players"]
                })
                if room.get("current_drawer") == pid:
                    t = room.get("round_task")
                    if t and not t.done():
                        t.cancel()
                    await pic_broadcast(room, {
                        "type": "info",
                        "msg": "Çizen çıktı, tur yeniden başlatılıyor."
                    })
                    room["round_task"] = asyncio.create_task(pic_start_round(room_id))

# ==========================
# TicTacToe (çok odalı)
# ==========================

def ttt_new_room(max_rounds: int = 1):
    return {
        "board": [None] * 9,
        "players": {},              # pid -> {name, mark, ws}
        "turn": "X",
        "scores": {"X": 0, "O": 0},
        "round": 1,
        "max_rounds": max_rounds,
        "host_pid": None
    }

def ttt_winner(b):
    wins = [
        (0,1,2),(3,4,5),(6,7,8),
        (0,3,6),(1,4,7),(2,5,8),
        (0,4,8),(2,4,6)
    ]
    for a,b2,c in wins:
        if b[a] and b[a] == b[b2] == b[c]:
            return b[a]
    if all(b):
        return "draw"
    return None

async def ttt_broadcast(room, payload: dict):
    msg = json.dumps(payload)
    dead = []
    for pid, pl in list(room["players"].items()):
        ws = pl["ws"]
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(pid)
    for pid in dead:
        room["players"].pop(pid, None)

async def ttt_push_state(room):
    host_mark = None
    host_pid = room.get("host_pid")
    if host_pid and host_pid in room["players"]:
        host_mark = room["players"][host_pid]["mark"]

    payload = {
        "type": "state",
        "board": room["board"],
        "turn": room["turn"],
        "round": room.get("round", 1),
        "maxRounds": room.get("max_rounds", 1),
        "scores": room.get("scores", {"X": 0, "O": 0}),
        "hostMark": host_mark
    }
    await ttt_broadcast(room, payload)

@app.websocket("/ws/ttt")
async def ttt_ws(ws: WebSocket):
    await ws.accept()
    pid = secrets.token_hex(3)
    room_id = None
    try:
        while True:
            data = json.loads(await ws.receive_text())
            typ = data.get("type")

            if typ == "join":
                room_id = data["roomId"]
                name = data.get("name", "anon")[:24]
                mode = data.get("mode", "join")

                if room_id not in ttt_rooms and mode != "create":
                    await ws_send(ws, {"type": "join_error","reason": "no_such_room"})
                    continue

                new_room = False
                if room_id not in ttt_rooms and mode == "create":
                    max_rounds = int(data.get("rounds", 1) or 1)
                    if max_rounds not in (1, 3, 5, 10):
                        max_rounds = 1
                    ttt_rooms[room_id] = ttt_new_room(max_rounds)
                    new_room = True

                room = ttt_rooms[room_id]

                if len(room["players"]) >= 2:
                    await ws_send(ws, {"type": "info","msg": "Oda dolu (2/2)"})
                    continue

                used_marks = [p["mark"] for p in room["players"].values()]
                mark = "X" if "X" not in used_marks else "O"

                room["players"][pid] = {"name": name,"mark": mark,"ws": ws}

                if new_room:
                    room["host_pid"] = pid

                await ws_send(ws, {"type": "joined","pid": pid,"mark": mark,"isHost": room.get("host_pid") == pid})
                await ttt_push_state(room)

            if typ == "move" and room_id:
                room = ttt_rooms.get(room_id)
                if not room or pid not in room["players"]:
                    continue
                mark = room["players"][pid]["mark"]
                if room["turn"] != mark:
                    continue
                idx = int(data.get("idx", -1))
                if idx < 0 or idx > 8 or room["board"][idx]:
                    continue

                room["board"][idx] = mark
                room["turn"] = "O" if mark == "X" else "X"

                w = ttt_winner(room["board"])
                if w:
                    if w != "draw":
                        room["scores"][w] = room["scores"].get(w, 0) + 1

                    current_round = room.get("round", 1)
                    max_rounds = room.get("max_rounds", 1)
                    msg = "Berabere!" if w == "draw" else f"Kazanan: {w}"
                    match_over = current_round >= max_rounds

                    result_payload = {
                        "type": "result",
                        "msg": msg,
                        "round": current_round,
                        "maxRounds": max_rounds,
                        "scores": room["scores"],
                        "matchOver": match_over
                    }
                    await ttt_broadcast(room, result_payload)

                    room["board"] = [None] * 9
                    room["turn"] = "X"

                    if match_over:
                        room["round"] = 1
                        room["scores"] = {"X": 0, "O": 0}
                    else:
                        room["round"] = current_round + 1

                    await ttt_push_state(room)
                else:
                    await ttt_push_state(room)

            if typ == "rematch" and room_id:
                room = ttt_rooms.get(room_id)
                if not room:
                    continue
                if room.get("host_pid") != pid:
                    await ws_send(ws, {"type": "info","msg": "Yeni seri başlatma yetkisi sadece oda sahibinde."})
                    continue

                room["board"] = [None] * 9
                room["turn"] = "X"
                room["scores"] = {"X": 0, "O": 0}
                room["round"] = 1

                await ttt_broadcast(room, {"type": "info","msg": "Oda sahibi yeni bir seri başlattı."})
                await ttt_push_state(room)

            if typ == "host_exit" and room_id:
                room = ttt_rooms.get(room_id)
                if not room:
                    continue

                if room.get("host_pid") != pid:
                    await ws_send(ws, {"type": "info","msg": "Odayı kapatma yetkisi sadece oda sahibinde."})
                    continue

                await ttt_broadcast(room, {"type": "host_left","msg": "Oda sahibi oyunu terk etti. Oda kapatılıyor."})

                for other_pid, pl in list(room["players"].items()):
                    if other_pid == pid:
                        continue
                    try:
                        await pl["ws"].close()
                    except:
                        pass

                ttt_rooms.pop(room_id, None)
                break

    except WebSocketDisconnect:
        pass
    finally:
        if room_id and room_id in ttt_rooms:
            room = ttt_rooms[room_id]
            if pid in room["players"]:
                room["players"].pop(pid, None)
            if not room["players"]:
                ttt_rooms.pop(room_id, None)
            else:
                if room.get("host_pid") == pid:
                    new_host = next(iter(room["players"].keys()), None)
                    room["host_pid"] = new_host

# ==========================
# Codenames
# ==========================
CN_WORDS = [
    "ELMA","ARABA","KÖPRÜ","AY","GÜNEŞ","BULUT","ROBOT","PENCERE","KALEM","ÇANTA",
    "KAMERA","DAĞ","DENİZ","BALIK","KEDİ","KÖPEK","UÇAK","TREN","MASA","SANDALYE",
    "ORMAN","HARİTA","PİZZA","LİMON","KALP","YILDIZ","OKUL","OYUN","DÜĞME","BİLGİSAYAR",
    "RADYO","TELEFON","BAHÇE","MÜZİK","SPOR","FUTBOL","ZİL","KAPI","ELDİVEN","KULAK",
    "BURUN","GÖZLÜK","İSKELE","TİLKİ","ASLAN","TAVŞAN","KAZAK","ELBİSE","KUPA","FİLM"
]

def cn_new_state_lobby():
    return {
        "phase":"lobby",
        "players":{},
        "spymaster": {"red":None,"blue":None}
    }

def cn_new_board():
    words = random.sample(CN_WORDS, 25)
    colors = ['red']*9 + ['blue']*8 + ['neut']*7 + ['ass']*1
    random.shuffle(colors)
    return {
        "phase":"play",
        "words": words,
        "colors": colors,
        "revealed": [],
        "turn": "red",
        "clue": {"word": None, "count": 0},
        "guessesLeft": 0,
        "players":{},
        "spymaster":{"red":None,"blue":None}
    }


async def cn_broadcast(room, payload):
    dead=[]
    for pid, pl in list(room["players"].items()):
        ws = pl["ws"]
        try: await ws.send_text(json.dumps(payload))
        except: dead.append(pid)
    for pid in dead: room["players"].pop(pid, None)

async def cn_push_lobby(room):
    lobby = {
        "phase":"lobby",
        "players": {pid: {"name":pl["name"],"team":pl.get("team"),"role":pl.get("role")} for pid,pl in room["players"].items()},
        "spymaster": room["spymaster"]
    }
    await cn_broadcast(room, {"type":"lobby_state","state":lobby})

async def cn_push_play(room):
    for pid, pl in list(room["players"].items()):
        ws=pl["ws"]
        try:
            if pl.get("role")=="spymaster":
                state = {
                    "words": room["words"], "colors": room["colors"],
                    "revealed": room["revealed"], "turn": room["turn"],
                    "clue": room["clue"], "guessesLeft": room["guessesLeft"]
                }
            else:
                colors_view=['neut']*25
                for i in room["revealed"]: colors_view[i]=room["colors"][i]
                state = {
                    "words": room["words"], "colors": colors_view,
                    "revealed": room["revealed"], "turn": room["turn"],
                    "clue": room["clue"], "guessesLeft": room["guessesLeft"]
                }
            payload = {"type":"state","state":state,"you":{"team":pl.get("team"),"role":pl.get("role")}}
            await ws.send_text(json.dumps(payload))
        except: pass

def cn_check_win(room):
    red_left = sum(1 for i,c in enumerate(room["colors"]) if c=='red' and i not in room["revealed"])
    blue_left = sum(1 for i,c in enumerate(room["colors"]) if c=='blue' and i not in room["revealed"])
    if red_left==0: return "red"
    if blue_left==0: return "blue"
    return None

def cn_requirements_ok(room):
    reds = [pl for pl in room["players"].values() if pl.get("team")=="red"]
    blues = [pl for pl in room["players"].values() if pl.get("team")=="blue"]
    red_spy = room["spymaster"]["red"] is not None
    blue_spy = room["spymaster"]["blue"] is not None
    red_ops = any(pl.get("role")=="operative" for pl in reds)
    blue_ops = any(pl.get("role")=="operative" for pl in blues)
    return red_spy and blue_spy and (red_ops or blue_ops) and len(room["players"])>=2

@app.websocket("/ws/codenames")
async def cn_ws(ws: WebSocket):
    await ws.accept()
    pid = secrets.token_hex(3)
    room_id = None
    try:
        while True:
            data = json.loads(await ws.receive_text())
            typ = data.get("type")

            if typ == "join":
                room_id = data["roomId"]
                name = data.get("name", "anon")[:24]
                mode = data.get("mode", "join")

                if room_id not in cn_rooms and mode != "create":
                    await ws_send(ws, {"type": "join_error","reason": "no_such_room"})
                    continue

                if room_id not in cn_rooms and mode == "create":
                    cn_rooms[room_id] = cn_new_state_lobby()

                room = cn_rooms[room_id]
                room["players"][pid] = {"name": name,"team": None,"role": None,"ws": ws}

                await ws_send(ws, {"type": "joined", "pid": pid})
                await cn_push_lobby(room)

            if typ=="set_team_role" and room_id:
                room = cn_rooms.get(room_id);
                if not room or room.get("phase")=="play": continue
                team = data.get("team")
                role = data.get("role")
                if team not in ("red","blue") or role not in ("spymaster","operative"):
                    continue
                if role=="spymaster":
                    if room["spymaster"][team] is not None and room["spymaster"][team]!=pid:
                        await ws_send(ws, {"type":"info","msg":"Bu takımın spymaster'ı dolu."})
                        continue
                    for t in ("red","blue"):
                        if room["spymaster"][t]==pid: room["spymaster"][t]=None
                    room["spymaster"][team]=pid
                else:
                    for t in ("red","blue"):
                        if room["spymaster"][t]==pid: room["spymaster"][t]=None

                room["players"][pid]["team"]=team
                room["players"][pid]["role"]=role
                await ws_send(ws, {"type":"you","team":team,"role":role})
                await cn_push_lobby(room)

            if typ=="start_game" and room_id:
                room = cn_rooms.get(room_id);
                if not room or room.get("phase")=="play": continue
                if not cn_requirements_ok(room):
                    await ws_send(ws, {"type":"info","msg":"Başlatmak için iki takımda da 1 spymaster ve oyuncular olmalı."})
                    continue
                play = cn_new_board()
                play["players"] = room["players"]
                play["spymaster"] = room["spymaster"]
                cn_rooms[room_id] = play
                await cn_push_play(play)

            if typ=="clue" and room_id:
                room = cn_rooms.get(room_id)
                if not room or room.get("phase")!="play": continue
                if room["spymaster"][room["turn"]] != pid:
                    await ws_send(ws, {"type":"info","msg":"İpucu verme yetkin yok"})
                    continue
                word = str(data.get("word","")).strip().upper()[:20]
                count = int(data.get("count",0))
                room["clue"] = {"word":word, "count":count}
                room["guessesLeft"] = max(0,count) + 1
                await cn_broadcast(room, {"type":"info","msg":f"İpucu: {word} ({count})"})
                await cn_push_play(room)

            if typ=="guess" and room_id:
                room = cn_rooms.get(room_id)
                if not room or room.get("phase")!="play": continue
                pl = room["players"].get(pid);
                if not pl or pl.get("role")!="operative" or pl.get("team")!=room["turn"]:
                    continue
                idx = int(data.get("idx",-1))
                if idx<0 or idx>=25 or idx in room["revealed"]: continue

                room["revealed"].append(idx)
                color = room["colors"][idx]

                if color=='ass':
                    winner = 'blue' if room["turn"]=='red' else 'red'
                    await cn_broadcast(room, {"type":"result","msg":f"SUİKAST! {winner.upper()} kazandı!"})
                    cn_rooms.pop(room_id, None); continue

                if color!=room["turn"]:
                    room["guessesLeft"]=0
                else:
                    if room["guessesLeft"]>0: room["guessesLeft"]-=1

                win = cn_check_win(room)
                if win:
                    await cn_broadcast(room, {"type":"result","msg":f"{win.upper()} kazandı!"})
                    cn_rooms.pop(room_id, None); continue

                if room["guessesLeft"]<=0:
                    room["turn"] = 'blue' if room["turn"]=='red' else 'red'
                    room["clue"] = {"word":None,"count":0}

                await cn_push_play(room)

            if typ=="end_turn" and room_id:
                room = cn_rooms.get(room_id)
                if not room or room.get("phase")!="play": continue
                pl = room["players"].get(pid)
                if not pl or pl.get("team") != room["turn"]:
                    continue
                room["turn"] = 'blue' if room["turn"]=='red' else 'red'
                room["clue"] = {"word":None,"count":0}
                room["guessesLeft"] = 0
                await cn_push_play(room)

    except WebSocketDisconnect:
        pass
    finally:
        if room_id and room_id in cn_rooms:
            room = cn_rooms[room_id]
            for t in ("red","blue"):
                if room.get("spymaster",{}).get(t)==pid:
                    room["spymaster"][t]=None
            if "players" in room and pid in room["players"]:
                room["players"].pop(pid, None)
            if not room.get("players"): cn_rooms.pop(room_id, None)

# ==========================
# Pixel War (Kare Kapmaca)
# ==========================
GRID_SIZE = 36
COLORS = ["#e74c3c", "#3498db", "#f1c40f", "#9b59b6", "#2ecc71", "#e67e22"]

async def pixel_timer(room_id):
    for i in range(30, -1, -1):
        if room_id not in pixel_rooms: return
        room = pixel_rooms[room_id]
        for p in room["players"]:
            try: await p["ws"].send_text(json.dumps({"type": "tick", "seconds": i}))
            except: pass
        await asyncio.sleep(1)

    if room_id in pixel_rooms:
        room = pixel_rooms[room_id]
        room["active"] = False
        counts = {}
        for c in room["board"]:
            if c: counts[c] = counts.get(c, 0) + 1

        winner_name = "Kimse"
        max_score = -1
        for p in room["players"]:
            score = counts.get(p["color"], 0)
            if score > max_score:
                max_score = score
                winner_name = p["name"]

        msg = {"type": "game_over", "winner": winner_name}
        for p in room["players"]:
            try: await p["ws"].send_text(json.dumps(msg))
            except: pass

def calculate_scores(room):
    counts = {}
    for c in room["board"]:
        if c: counts[c] = counts.get(c, 0) + 1
    scores = {}
    for p in room["players"]:
        scores[p["name"]] = counts.get(p["color"], 0)
    return scores

@app.websocket("/ws/pixelwar")
async def pixel_ws(ws: WebSocket):
    await ws.accept()
    room_id = None
    pid = secrets.token_hex(3)
    try:
        while True:
            data = json.loads(await ws.receive_text())
            typ = data.get("type")

            if typ == "join":
                room_id = data["roomId"]
                name = data.get("name", "Anonim")
                if room_id not in pixel_rooms:
                    pixel_rooms[room_id] = {"players": [], "board": [None]*GRID_SIZE, "active": False}

                room = pixel_rooms[room_id]
                color_idx = len(room["players"]) % len(COLORS)
                my_color = COLORS[color_idx]

                room["players"].append({"pid": pid, "name": name, "color": my_color, "ws": ws})
                await ws.send_text(json.dumps({"type": "welcome", "color": my_color}))

                scores = calculate_scores(room)
                await ws.send_text(json.dumps({"type": "state", "board": room["board"], "scores": scores}))

            elif typ == "start" and room_id:
                room = pixel_rooms[room_id]
                if not room["active"]:
                    room["active"] = True
                    room["board"] = [None] * GRID_SIZE
                    asyncio.create_task(pixel_timer(room_id))
                    scores = calculate_scores(room)
                    broadcast = json.dumps({"type": "state", "board": room["board"], "scores": scores})
                    for p in room["players"]: await p["ws"].send_text(broadcast)

            elif typ == "click" and room_id:
                room = pixel_rooms[room_id]
                if not room["active"]: continue
                player = next((p for p in room["players"] if p["pid"] == pid), None)
                if player:
                    idx = int(data.get("idx", 0))
                    if 0 <= idx < GRID_SIZE:
                        room["board"][idx] = player["color"]
                        scores = calculate_scores(room)
                        broadcast = json.dumps({"type": "state", "board": room["board"], "scores": scores})
                        for p in room["players"]: await p["ws"].send_text(broadcast)

    except WebSocketDisconnect:
        if room_id and room_id in pixel_rooms:
            room = pixel_rooms[room_id]
            room["players"] = [p for p in room["players"] if p["pid"] != pid]
            if not room["players"]: del pixel_rooms[room_id]


# ==========================
# LIAR'S BAR
# ==========================
liars_rooms = {}

def liars_new_room():
    return {
        "players": {},           # pid -> {name, ws, cards[], alive, position}
        "phase": "lobby",        # lobby, playing, roulette, game_over
        "turn": None,            # Sıradaki oyuncu pid
        "turn_order": [],        # Oyuncu sırası
        "current_claim": None,   # {card: "Q/K/A/JOKER", count: 1-4, pid: ""}
        "pile": [],              # Masadaki kartlar {card: "Q/K/A/JOKER", pid: ""}
        "roulette": None,        # {chamber: 0-5, current: 0, victim: pid}
        "deck": []               # Kalan kartlar
    }

def liars_create_deck():
    """32 kart: Q,K,A'dan 10'ar + 2 Joker"""
    deck = []
    for card_type in ['Q', 'K', 'A']:
        deck.extend([card_type] * 10)
    deck.extend(['JOKER'] * 2)
    random.shuffle(deck)
    return deck

async def liars_broadcast(room, payload):
    msg = json.dumps(payload)
    dead = []
    for pid, pl in list(room["players"].items()):
        ws = pl["ws"]
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(pid)
    for pid in dead:
        room["players"].pop(pid, None)

async def liars_push_state(room):
    """Her oyuncuya kendi kartlarını ve genel durumu gönder"""
    alive_players = {pid: {"name": pl["name"], "alive": pl["alive"], "card_count": len(pl["cards"]), "position": pl["position"], "shots_used": pl.get("shots_used", 0)}
                     for pid, pl in room["players"].items()}

    for pid, pl in room["players"].items():
        ws = pl["ws"]
        try:
            # Sadece hayattaysa kendi kartlarını göster, ölüyse boş liste
            my_cards = pl["cards"] if pl["alive"] else []

            state = {
                "type": "state",
                "phase": room["phase"],
                "players": alive_players,
                "my_cards": my_cards,
                "my_alive": pl["alive"],
                "turn": room["turn"],
                "current_claim": room["current_claim"],
                "pile_count": len(room["pile"]),
                "round_card": room.get("round_card")  # Turda atılacak kart
            }
            await ws.send_text(json.dumps(state))
        except:
            pass

def liars_start_game(room):
    """Oyunu başlat - kartları dağıt"""
    if len(room["players"]) < 2:
        return False

    # Desteden oluştur ve karıştır
    room["deck"] = liars_create_deck()

    # Her oyuncuya 8 kart dağıt
    pids = list(room["players"].keys())
    for i, pid in enumerate(pids):
        room["players"][pid]["cards"] = [room["deck"].pop() for _ in range(8)]
        room["players"][pid]["alive"] = True
        room["players"][pid]["position"] = i
        room["players"][pid]["shots_used"] = 0  # Kullanılan mermi sayısı

    room["turn_order"] = pids.copy()
    room["turn"] = random.choice(pids)  # Rastgele oyuncudan başla
    room["phase"] = "playing"
    room["pile"] = []
    room["current_claim"] = None
    room["round_card"] = random.choice(["Q", "K", "A"])  # Turda atılacak kart türü

    return True

def liars_next_turn(room):
    """Sıradaki canlı oyuncuya geç"""
    if not room["turn_order"]:
        return

    current_idx = room["turn_order"].index(room["turn"]) if room["turn"] in room["turn_order"] else -1

    for i in range(1, len(room["turn_order"]) + 1):
        next_idx = (current_idx + i) % len(room["turn_order"])
        next_pid = room["turn_order"][next_idx]
        if room["players"][next_pid]["alive"]:
            room["turn"] = next_pid
            return

    # Hiç canlı oyuncu kalmadıysa
    room["phase"] = "game_over"

def liars_check_winner(room):
    """Kazananı kontrol et"""
    alive = [pid for pid, pl in room["players"].items() if pl["alive"]]
    if len(alive) == 1:
        room["phase"] = "game_over"
        return alive[0]
    return None

async def liars_start_roulette(room, victim_pid):
    """Rusça rulet başlat"""
    # Kullanılan mermi sayısına göre şans hesapla
    shots_used = room["players"][victim_pid].get("shots_used", 0)
    # Kalan namlu sayısı: 6 - (shots_used % 6)
    remaining_chambers = 6 - (shots_used % 6)

    # Mermi pozisyonu: 0 ile (remaining_chambers - 1) arası
    chamber = random.randint(0, remaining_chambers - 1) if remaining_chambers > 0 else 0

    room["roulette"] = {
        "chamber": chamber,
        "current": 0,
        "victim": victim_pid,
        "remaining_chambers": remaining_chambers
    }
    room["phase"] = "roulette"

    await liars_broadcast(room, {
        "type": "roulette_start",
        "victim": victim_pid,
        "victim_name": room["players"][victim_pid]["name"],
        "chamber": 1,  # İlk çekiş
        "shots_used": shots_used  # Şu ana kadar kullanılan mermi
    })

async def liars_pull_trigger(room):
    """Tetiği çek"""
    roulette = room["roulette"]
    victim_pid = roulette["victim"]
    remaining_chambers = roulette.get("remaining_chambers", 6)

    # Mevcut çekiş remaining_chambers'ı aştıysa, kesinlikle ölme
    is_shot = roulette["current"] == roulette["chamber"] and roulette["current"] < remaining_chambers
    roulette["current"] += 1

    # Mermi kullanımını artır
    if victim_pid in room["players"]:
        room["players"][victim_pid]["shots_used"] = room["players"][victim_pid].get("shots_used", 0) + 1

    if is_shot:
        # Oyuncu öldü
        room["players"][victim_pid]["alive"] = False
        room["players"][victim_pid]["cards"] = []  # Kartlarını temizle

        await liars_broadcast(room, {
            "type": "roulette_result",
            "victim": victim_pid,
            "shot": True,
            "chamber": roulette["current"],  # Kaçıncı çekişte patladı
            "shots_used": room["players"][victim_pid].get("shots_used", 0)  # Toplam kullanılan mermi
        })

        await asyncio.sleep(2)  # Animasyon için bekleme

        # Kazanan var mı?
        winner = liars_check_winner(room)
        if winner:
            await liars_broadcast(room, {
                "type": "game_over",
                "winner": winner,
                "winner_name": room["players"][winner]["name"]
            })
        else:
            # Oyuna devam - yeni tur, yeni kartlar dağıt
            room["phase"] = "playing"
            caller_pid = room["roulette"].get("caller")  # Blöf diyen kişi
            room["roulette"] = None
            room["pile"] = []
            room["current_claim"] = None
            room["round_card"] = random.choice(["Q", "K", "A"])  # Yeni kart türü

            # Canlı oyunculara yeni kartlar dağıt (komple yenile)
            alive_players = [pid for pid, pl in room["players"].items() if pl["alive"]]

            # Yeni deste oluştur
            room["deck"] = liars_create_deck()

            # Her canlı oyuncuya yeni 8 kart dağıt (eskilerini sil)
            for pid in alive_players:
                room["players"][pid]["cards"] = []
                for _ in range(8):
                    if room["deck"]:
                        room["players"][pid]["cards"].append(room["deck"].pop())

            # Sıra blöf diyende (eğer hayattaysa)
            if caller_pid and caller_pid in alive_players:
                room["turn"] = caller_pid
            else:
                liars_next_turn(room)
            await liars_push_state(room)
    else:
        # Kurtuldu
        await liars_broadcast(room, {
            "type": "roulette_result",
            "victim": victim_pid,
            "shot": False,
            "chamber": roulette["current"],  # Şu anki pozisyon (kaçıncı çekiş)
            "shots_used": room["players"][victim_pid].get("shots_used", 0)  # Toplam kullanılan mermi
        })

        await asyncio.sleep(1)  # Animasyon için bekleme

        # Oyuna devam et - yeni tur, yeni kartlar dağıt
        room["phase"] = "playing"
        caller_pid = room["roulette"].get("caller")  # Blöf diyen kişi
        room["roulette"] = None
        room["pile"] = []
        room["current_claim"] = None
        room["round_card"] = random.choice(["Q", "K", "A"])  # Yeni kart türü

        # Canlı oyunculara yeni kartlar dağıt (komple yenile)
        alive_players = [pid for pid, pl in room["players"].items() if pl["alive"]]

        # Yeni deste oluştur
        room["deck"] = liars_create_deck()

        # Her canlı oyuncuya yeni 8 kart dağıt (eskilerini sil)
        for pid in alive_players:
            room["players"][pid]["cards"] = []
            for _ in range(8):
                if room["deck"]:
                    room["players"][pid]["cards"].append(room["deck"].pop())

        # Sıra blöf diyende (eğer hayattaysa)
        if caller_pid and caller_pid in alive_players:
            room["turn"] = caller_pid
        else:
            liars_next_turn(room)
        await liars_push_state(room)

@app.websocket("/ws/liars")
async def liars_ws(ws: WebSocket):
    await ws.accept()
    pid = secrets.token_hex(3)
    room_id = None

    try:
        while True:
            data = json.loads(await ws.receive_text())
            typ = data.get("type")

            if typ == "join":
                room_id = data["roomId"]
                name = data.get("name", "Oyuncu")[:24]
                mode = data.get("mode", "join")

                if room_id not in liars_rooms and mode != "create":
                    await ws_send(ws, {"type": "join_error", "reason": "no_such_room"})
                    continue

                if room_id in liars_rooms and mode == "create":
                    await ws_send(ws, {"type": "join_error", "reason": "room_exists", "msg": "Bu oda zaten mevcut!"})
                    continue

                if room_id not in liars_rooms:
                    liars_rooms[room_id] = liars_new_room()

                room = liars_rooms[room_id]

                if room["phase"] != "lobby":
                    await ws_send(ws, {"type": "join_error", "reason": "game_in_progress", "msg": "Oyun devam ediyor!"})
                    continue

                if len(room["players"]) >= 6:
                    await ws_send(ws, {"type": "join_error", "reason": "room_full", "msg": "Oda dolu!"})
                    continue

                existing_names = [pl["name"] for pl in room["players"].values()]
                if name in existing_names:
                    await ws_send(ws, {"type": "join_error", "reason": "name_taken", "msg": f"'{name}' ismi kullanılıyor!"})
                    continue

                room["players"][pid] = {
                    "name": name,
                    "ws": ws,
                    "cards": [],
                    "alive": True,
                    "position": 0
                }

                await ws_send(ws, {"type": "joined", "pid": pid})
                await liars_broadcast(room, {
                    "type": "lobby_update",
                    "players": {p: {"name": room["players"][p]["name"]} for p in room["players"]}
                })

            elif typ == "start_game" and room_id:
                room = liars_rooms.get(room_id)
                if not room or room["phase"] != "lobby":
                    continue

                if len(room["players"]) < 2:
                    await ws_send(ws, {"type": "info", "msg": "En az 2 oyuncu gerekli!"})
                    continue

                if liars_start_game(room):
                    await liars_push_state(room)

            elif typ == "play_cards" and room_id:
                room = liars_rooms.get(room_id)
                if not room or room["phase"] != "playing" or room["turn"] != pid:
                    continue

                card_indices = data.get("card_indices", [])  # Atılacak kartların indeksleri

                if not card_indices or len(card_indices) > 4:
                    continue

                # Sunucu tarafından belirlenen kart türünü kullan
                claimed_card = room.get("round_card", "Q")

                player = room["players"][pid]

                # Kartları kontrol et ve ata
                played_cards = []
                for idx in sorted(card_indices, reverse=True):
                    if 0 <= idx < len(player["cards"]):
                        card = player["cards"].pop(idx)
                        played_cards.append(card)
                        room["pile"].append({"card": card, "pid": pid})

                room["current_claim"] = {
                    "card": claimed_card,
                    "count": len(played_cards),
                    "pid": pid
                }

                # Tüm kartları bitirdiyse kazandı
                if len(player["cards"]) == 0:
                    room["phase"] = "game_over"
                    await liars_broadcast(room, {
                        "type": "game_over",
                        "winner": pid,
                        "winner_name": player["name"]
                    })
                else:
                    liars_next_turn(room)
                    await liars_push_state(room)
                    await liars_broadcast(room, {
                        "type": "play_made",
                        "player": pid,
                        "player_name": player["name"],
                        "claim": room["current_claim"]
                    })

            elif typ == "call_liar" and room_id:
                room = liars_rooms.get(room_id)
                if not room or room["phase"] != "playing" or not room["current_claim"]:
                    continue

                caller_player = room["players"].get(pid)
                if not caller_player or not caller_player["alive"]:
                    continue

                claim = room["current_claim"]
                claimer_pid = claim["pid"]
                claimed_card = claim["card"]

                # Oyuncu kendine yalan diyemez
                if pid == claimer_pid:
                    await ws_send(ws, {"type": "info", "msg": "Kendine yalan diyemezsin!"})
                    continue

                # Pile'daki son atılan kartları kontrol et
                last_cards = room["pile"][-claim["count"]:]

                # Joker ve iddia edilen kartı kabul et
                is_valid = all(c["card"] == claimed_card or c["card"] == "JOKER" for c in last_cards)

                await liars_broadcast(room, {
                    "type": "liar_called",
                    "caller": pid,
                    "caller_name": caller_player["name"],
                    "claimer": claimer_pid,
                    "cards_revealed": [c["card"] for c in last_cards],
                    "valid": is_valid
                })

                # Rusça rulet
                victim = claimer_pid if not is_valid else pid
                await liars_start_roulette(room, victim)
                # Blöf diyen kişiyi kaydet
                room["roulette"]["caller"] = pid

            elif typ == "pull_trigger" and room_id:
                room = liars_rooms.get(room_id)
                if not room or room["phase"] != "roulette":
                    continue

                if room["roulette"]["victim"] != pid:
                    continue

                await liars_pull_trigger(room)

    except WebSocketDisconnect:
        pass
    finally:
        if room_id and room_id in liars_rooms:
            room = liars_rooms[room_id]
            room["players"].pop(pid, None)

            if not room["players"]:
                liars_rooms.pop(room_id, None)
            else:
                if room["phase"] == "lobby":
                    await liars_broadcast(room, {
                        "type": "lobby_update",
                        "players": {p: {"name": room["players"][p]["name"]} for p in room["players"]}
                    })

# ==========================
# Sumo Bash (yuvarlak arena mini game)
# ==========================

def make_sumo_room(room_id: str) -> dict:
    return {
        "roomId": room_id,
        "players": {},      # pid -> {name, ws, x, y, alive, color, wins}
        "phase": "waiting", # "waiting" | "playing" | "finished"
        "arena_radius": 200.0,
        "min_radius": 90.0,
        "shrink_speed": 12.0,   # saniyede kaç px küçülsün
        "last_update": None,
        "host_pid": None,
    }

def sumo_random_color() -> str:
    palette = ["#f97316", "#22c55e", "#0ea5e9", "#a855f7", "#facc15", "#f97373"]
    return random.choice(palette)

def sumo_random_spawn(room: dict) -> tuple[float, float]:
    r = room.get("arena_radius", 200.0) * 0.55
    ang = random.uniform(0, math.tau)
    return math.cos(ang) * r, math.sin(ang) * r

async def sumo_info(room: dict, text: str):
    msg = json.dumps({"type": "info", "msg": text})
    for p in list(room["players"].values()):
        ws: WebSocket = p.get("ws")
        if not ws:
            continue
        try:
            await ws.send_text(msg)
        except Exception:
            pass

def sumo_broadcast_state(room: dict, info: str | None = None, winner: str | None = None):
    players_view = {}
    for pid, p in room["players"].items():
        players_view[pid] = {
            "name": p["name"],
            "x": float(p.get("x", 0.0)),
            "y": float(p.get("y", 0.0)),
            "color": p.get("color"),
            "alive": bool(p.get("alive", True)),
            "wins": int(p.get("wins", 0)),
        }

    msg = {
        "type": "state",
        "phase": room.get("phase", "waiting"),
        "arena": {"radius": float(room.get("arena_radius", 200.0))},
        "players": players_view,
        "winner": winner,
        "canStart": len(room["players"]) >= 2 and room.get("phase") in ("waiting", "finished"),
    }
    if info:
        msg["info"] = info

    txt = json.dumps(msg)
    for p in list(room["players"].values()):
        ws: WebSocket = p.get("ws")
        if not ws:
            continue
        try:
            asyncio.create_task(ws.send_text(txt))
        except Exception:
            pass

def sumo_update_arena_shrink(room: dict):
    """Oyun oynanırken süre geçtikçe arenayı küçült."""
    if room.get("phase") != "playing":
        room["last_update"] = None
        return
    now = datetime.utcnow().timestamp()
    last = room.get("last_update")
    room["last_update"] = now
    if last is None:
        return
    dt = max(0.0, now - last)
    shrink_speed = float(room.get("shrink_speed", 12.0))
    min_r = float(room.get("min_radius", 90.0))
    r = float(room.get("arena_radius", 200.0))
    if r <= min_r:
        room["arena_radius"] = min_r
        return
    r -= shrink_speed * dt
    if r < min_r:
        r = min_r
    room["arena_radius"] = r

def sumo_resolve_collisions(room: dict, mover_pid: str, ball_radius: float = 18.0):
    """
    Çarpışan oyuncuları birbirinden güçlü şekilde iter.
    Vuran oyuncu az, vurulan oyuncu çok geri gider (knockback).
    """
    p = room["players"].get(mover_pid)
    if not p or not p.get("alive", True):
        return

    for pid2, other in room["players"].items():
        if pid2 == mover_pid:
            continue
        if not other.get("alive", True):
            continue

        # Vurandan hedefe doğru vektör
        dx = other["x"] - p["x"]
        dy = other["y"] - p["y"]
        dist = math.hypot(dx, dy)
        if dist == 0:
            dist = 0.001  # sıfıra bölme olmasın

        min_dist = ball_radius * 2.0  # iki topun normalde olması gereken minimum mesafe
        if dist < min_dist:
            # Ne kadar iç içe girdik? + ekstra bonus itme ekleyelim ki hissedilsin
            overlap = (min_dist - dist) + 12.0

            ux = dx / dist  # vurandan hedefe doğru birim vektör
            uy = dy / dist

            # Vurulan: ileri doğru güçlü itilsin
            hit_push = overlap * 0.9
            # Vuran: geri doğru hafifçe geri sekecek
            self_push = overlap * 0.35

            # Vuranı geri doğru it (tam tersi yönde)
            p["x"] -= ux * self_push
            p["y"] -= uy * self_push

            # Vurulanı ileri doğru it
            other["x"] += ux * hit_push
            other["y"] += uy * hit_push

def sumo_check_eliminations(room: dict):
    radius = float(room.get("arena_radius", 200.0))
    for p in room["players"].values():
        if not p.get("alive", True):
            continue
        dist = math.hypot(p.get("x", 0.0), p.get("y", 0.0))
        if dist > radius:
            p["alive"] = False

    alive = [p for p in room["players"].values() if p.get("alive", False)]
    if room.get("phase") == "playing" and len(alive) <= 1:
        room["phase"] = "finished"
        winner = None
        if alive:
            alive[0]["wins"] = alive[0].get("wins", 0) + 1
            winner = alive[0]["name"]
        return winner
    return None


@app.websocket("/ws/sumobash")
async def ws_sumobash(ws: WebSocket):
    await ws.accept()
    pid = secrets.token_hex(4)
    room_id = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            typ = msg.get("type")

            # ---- join ----
            if typ == "join":
                room_id = (msg.get("roomId") or "").strip()
                name = (msg.get("name") or "anon").strip() or "anon"

                if not room_id:
                    await ws.send_text(json.dumps({"type": "join_error", "reason": "no_room"}))
                    continue

                room = sumo_rooms.get(room_id)
                if room is None:
                    room = make_sumo_room(room_id)
                    sumo_rooms[room_id] = room

                is_host = False
                if not room["players"]:
                    room["host_pid"] = pid
                    is_host = True

                x, y = sumo_random_spawn(room)
                room["players"][pid] = {
                    "name": name,
                    "ws": ws,
                    "x": x,
                    "y": y,
                    "alive": True,
                    "color": sumo_random_color(),
                    "wins": 0,
                }

                await ws.send_text(json.dumps({
                    "type": "joined",
                    "pid": pid,
                    "roomId": room_id,
                    "isHost": is_host,
                    "name": name,
                }))

                await sumo_info(room, f"{name} odaya katıldı.")
                sumo_broadcast_state(room, info="Oyuncular hazır olduğunda host oyunu başlatabilir.")
                continue

            # join gelmediyse
            if room_id is None:
                await ws.send_text(json.dumps({"type": "info", "msg": "Önce join gönder."}))
                continue

            room = sumo_rooms.get(room_id)
            if not room:
                await ws.send_text(json.dumps({"type": "info", "msg": "Oda bulunamadı."}))
                continue

            # Her mesajda arena küçülmesini güncelle
            sumo_update_arena_shrink(room)

            # ---- start ----
            if typ == "start":
                if pid != room.get("host_pid"):
                    await ws.send_text(json.dumps({"type": "info", "msg": "Yalnızca host oyunu başlatabilir."}))
                    continue
                if len(room["players"]) < 2:
                    await ws.send_text(json.dumps({"type": "info", "msg": "En az 2 oyuncu gerekli."}))
                    continue

                room["phase"] = "playing"
                room["arena_radius"] = 200.0
                room["last_update"] = None
                for p in room["players"].values():
                    p["alive"] = True
                    p["x"], p["y"] = sumo_random_spawn(room)

                sumo_broadcast_state(room, info="Oyun başladı! Arena yavaş yavaş daralıyor, düşmemeye çalışın.")
                continue

            # ---- reset ----
            if typ == "reset":
                if pid != room.get("host_pid"):
                    await ws.send_text(json.dumps({"type": "info", "msg": "Yalnızca host yeni tur başlatabilir."}))
                    continue
                room["phase"] = "waiting"
                room["arena_radius"] = 200.0
                room["last_update"] = None
                for p in room["players"].values():
                    p["alive"] = True
                    p["x"], p["y"] = sumo_random_spawn(room)
                sumo_broadcast_state(room, info="Yeni tur için hazır. Host oyunu başlatabilir.")
                continue

            # ---- move ----
            if typ == "move":
                if room.get("phase") != "playing":
                    continue
                p = room["players"].get(pid)
                if not p or not p.get("alive", True):
                    continue

                try:
                    nx = float(msg.get("x", p["x"]))
                    ny = float(msg.get("y", p["y"]))
                except (TypeError, ValueError):
                    continue

                p["x"] = nx
                p["y"] = ny

                sumo_resolve_collisions(room, pid)
                winner = sumo_check_eliminations(room)

                if winner:
                    sumo_broadcast_state(room, info=f"Tur bitti! Kazanan: {winner}", winner=winner)
                else:
                    sumo_broadcast_state(room)
                continue

    except WebSocketDisconnect:
        pass
    except Exception:
        # loglamak istersen buraya print ya da logger koyabilirsin
        pass
    finally:
        if room_id and room_id in sumo_rooms:
            room = sumo_rooms[room_id]
            player = room["players"].pop(pid, None)
            if player:
                try:
                    asyncio.create_task(sumo_info(room, f"{player['name']} oyundan ayrıldı."))
                except Exception:
                    pass

            if pid == room.get("host_pid"):
                new_host = next(iter(room["players"]), None)
                room["host_pid"] = new_host

            if not room["players"]:
                sumo_rooms.pop(room_id, None)
            else:
                sumo_broadcast_state(room, info="Bir oyuncu oyundan ayrıldı.")


# ==========================
# Health / Rooms
# ==========================
@app.get("/health")
def health():
    return {"status":"ok", "time": datetime.utcnow().isoformat()+"Z"}

@app.get("/rooms")
def list_rooms():
    out=[]
    for rid, r in pic_rooms.items():
        out.append({"game":"pictionary","roomId":rid,"players":len(r["players"]),"started":r.get("started",False),"secondsLeft":r.get("seconds_left",0)})
    for rid, r in ttt_rooms.items():
        out.append({"game":"ttt","roomId":rid,"players":len(r["players"])})
    for rid, r in sumo_rooms.items():
        out.append({
            "game": "sumobash",
            "roomId": rid,
            "players": len(r["players"]),
            "phase": r.get("phase", "waiting")
        })


    for rid, r in cn_rooms.items():
        if r.get("phase")=="lobby":
            out.append({"game":"codenames","roomId":rid,"phase":"lobby","players":len(r["players"]),"spies":r.get("spymaster",{})})
        else:
            out.append({"game":"codenames","roomId":rid,"phase":"play","turn":r["turn"]})
    return JSONResponse(out)
# ====== Statik Dosyalar (HTML Oyunlar) ======
BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

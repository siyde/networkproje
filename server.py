# server.py — Game Hub WS Sunucusu (Pictionary + TTT + Codenames + PixelWar)
import asyncio, json, secrets, random, re, os
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

# ====== Statik Dosyalar (HTML Oyunlar) ======
BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

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

pic_rooms: Dict[str, dict] = {}

def mask_word(w): return " ".join(["_" if ch!=" " else " " for ch in w])

async def ws_send(ws, payload): await ws.send_text(json.dumps(payload))

async def pic_broadcast(room, payload):
    msg = json.dumps(payload)
    dead=[]
    for ws in list(room["clients"]):
        try: await ws.send_text(msg)
        except: dead.append(ws)
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
            "invite_key": None
        }
    return pic_rooms[room_id]

def pic_next_drawer(room):
    if not room["drawer_order"]: return None
    pid = room["drawer_order"][room["drawer_idx"] % len(room["drawer_order"])]
    room["drawer_idx"] = (room["drawer_idx"] + 1) % len(room["drawer_order"])
    return pid

async def pic_state_push(room_id):
    room = pic_rooms.get(room_id)
    if not room: return
    for ws in list(room["clients"]):
        pid = getattr(ws, "state_pid", None)
        if room["chosen"] and room["word"]:
            word_view = room["word"] if pid == room.get("current_drawer") else mask_word(room["word"])
        else:
            word_view = "(kelime seçiliyor)" if pid == room.get("current_drawer") else ""
        try:
            await ws.send_text(json.dumps({
                "type":"state",
                "players": room["players"],
                "drawer": room.get("current_drawer"),
                "word": word_view,
                "secondsLeft": room.get("seconds_left",0),
                "strokes": room["strokes"],
                "started": room["started"]
            }))
        except:
            room["clients"].discard(ws)

async def pic_start_round(room_id):
    room = pic_rooms.get(room_id)
    if not room or len(room["players"]) < 2:
        room["started"]=False; room["word"]=None; room["strokes"].clear(); room["seconds_left"]=0
        room["choices"]=None; room["chosen"]=False
        await pic_broadcast(room, {"type":"info","msg":"Yeni tur için en az 2 oyuncu gerekli."})
        await pic_state_push(room_id); return

    drawer = pic_next_drawer(room)
    room["current_drawer"]=drawer
    room["strokes"].clear()
    room["started"]=True
    room["seconds_left"]=0
    room["chosen"]=False
    room["word"]=None
    room["choices"]=random.sample(PIC_WORDS, 3)

    await pic_broadcast(room, {"type":"round_start","drawer":drawer})
    await pic_state_push(room_id)

    ws = room["ws_by_pid"].get(drawer)
    if ws: await ws_send(ws, {"type":"choose_word","choices":room["choices"],"timeout":CHOICE_SECONDS})

    try:
        for _ in range(CHOICE_SECONDS):
            await asyncio.sleep(1)
            if room["chosen"]: break
        if not room["chosen"] and room["choices"]:
            room["word"] = random.choice(room["choices"])
            room["chosen"]=True
            await pic_broadcast(room, {"type":"info","msg":"Kelime otomatik seçildi."})
    except asyncio.CancelledError:
        return

    room["seconds_left"]=ROUND_SECONDS
    await pic_state_push(room_id)

    try:
        while room["seconds_left"]>0:
            await asyncio.sleep(1)
            room["seconds_left"]-=1
            if room["seconds_left"]%5==0 or room["seconds_left"]<=5:
                await pic_state_push(room_id)
        await pic_broadcast(room, {"type":"round_end","result":"timeup","word":room["word"]})
    except asyncio.CancelledError:
        return
    await asyncio.sleep(INTERMISSION)
    room["round_task"] = asyncio.create_task(pic_start_round(room_id))

async def pic_end_round_with_winner(room_id, winner_pid):
    room = pic_rooms.get(room_id)
    if not room: return
    room["players"][winner_pid]["score"] += 10
    d = room.get("current_drawer")
    if d and d in room["players"]: room["players"][d]["score"] += 5
    await pic_broadcast(room, {"type":"round_end","result":"guessed","winner":winner_pid,"word":room["word"]})
    task = room.get("round_task")
    if task and not task.done(): task.cancel()
    await asyncio.sleep(INTERMISSION)
    room["round_task"] = asyncio.create_task(pic_start_round(room_id))

@app.websocket("/ws/pictionary")
async def pictionary_ws(ws: WebSocket):
    await ws.accept()
    room_id=None
    pid=secrets.token_hex(3)
    ws.state_pid=pid
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
                    await ws_send(ws, {"type": "join_error","reason": "no_such_room"})
                    continue

                new_room = room_id not in pic_rooms
                room = pic_room(room_id)

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

            elif typ=="choose_word" and room_id:
                room=pic_rooms.get(room_id)
                if not room or room.get("current_drawer")!=pid: continue
                choice = str(data.get("choice","")).strip()
                if room.get("choices") and choice in room["choices"]:
                    room["word"]=choice; room["chosen"]=True
                    await pic_broadcast(room, {"type":"info","msg":"Kelime seçildi!"})
                    await pic_state_push(room_id)

            elif typ=="leave" and room_id:
                break

            elif typ=="stroke" and room_id:
                room=pic_rooms.get(room_id)
                if not room or room.get("current_drawer")!=pid or not room.get("chosen"): continue
                s={"x0":data["x0"],"y0":data["y0"],"x1":data["x1"],"y1":data["y1"],"w":data.get("w",2),"c":data.get("c","#000")}
                room["strokes"].append(s)
                await pic_broadcast(room, {"type":"stroke","stroke":s})

            elif typ=="clear" and room_id:
                room=pic_rooms.get(room_id)
                if not room or room.get("current_drawer")!=pid or not room.get("chosen"): continue
                room["strokes"].clear()
                await pic_broadcast(room, {"type":"clear"})

            elif typ=="chat" and room_id:
                room=pic_rooms.get(room_id);
                if not room: continue
                text=str(data.get("text",""))[:200]
                if room.get("word") and room.get("chosen") and text.strip():
                    norm=lambda s: re.sub(r"\s+","", s.lower())
                    if norm(text)==norm(room["word"]):
                        await pic_broadcast(room, {"type":"guess","pid":pid,"name":room["players"][pid]["name"],"correct":True})
                        await pic_end_round_with_winner(room_id,pid)
                        continue
                await pic_broadcast(room, {"type":"chat","pid":pid,"name":room["players"][pid]["name"],"text":text})
    except WebSocketDisconnect:
        pass
    finally:
        if room_id and room_id in pic_rooms:
            room=pic_rooms[room_id]
            info=room["players"].pop(pid, None)
            room["clients"].discard(ws)
            room["ws_by_pid"].pop(pid, None)
            if pid in room["drawer_order"]: room["drawer_order"].remove(pid)
            if not room["clients"]:
                t=room.get("round_task")
                if t and not t.done(): t.cancel()
                pic_rooms.pop(room_id,None)
            else:
                await pic_broadcast(room, {"type":"system","msg":f"{(info or {}).get('name','?')} ayrıldı","players":room["players"]})
                if room.get("current_drawer")==pid:
                    t=room.get("round_task")
                    if t and not t.done(): t.cancel()
                    await pic_broadcast(room, {"type":"info","msg":"Çizen çıktı, tur yeniden başlatılıyor."})
                    room["round_task"]=asyncio.create_task(pic_start_round(room_id))

# ==========================
# TicTacToe (çok odalı)
# ==========================
ttt_rooms = {}  # roomId -> room state dict

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

cn_rooms = {}  # roomId -> state

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
pixel_rooms = {}
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

    for rid, r in cn_rooms.items():
        if r.get("phase")=="lobby":
            out.append({"game":"codenames","roomId":rid,"phase":"lobby","players":len(r["players"]),"spies":r.get("spymaster",{})})
        else:
            out.append({"game":"codenames","roomId":rid,"phase":"play","turn":r["turn"]})
    return JSONResponse(out)

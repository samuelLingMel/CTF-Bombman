"""Pygame client: networking menu (host / join) + free-move gameplay.

The client never decides its own position - it sends input intent to the
server and renders whatever state the server broadcasts back.
"""

import socket
import threading
import urllib.request

import pygame

import server as server_module
import sprites as sprites_module
from shared import settings
from shared.protocol import send_message, MessageReader


class NetworkClient:
    def __init__(self):
        self.sock = None
        self.reader = None
        self.player_id = None
        self.name = None
        self.team = None
        self.phase = "lobby"  # "lobby" | "playing" | "finished"
        self.host_ended = False  # True if the host chose "Back to Main Menu" (vs. a real disconnect)
        self.players = {}
        self.prev_players = {}  # previous "state" snapshot, for interpolating render position
        self.state_tick_ms = 0  # local time (ms) the current snapshot arrived
        self.prev_tick_ms = 0  # local time (ms) the previous snapshot arrived
        self.bombs = []
        self.blasts = []
        self.power_ups = []
        self.recent_destructions = []  # (col, row) cells, drained each frame for the crumble animation
        self.flags = {}
        self.scores = {}
        self.winner = None
        self.hard_walls = set()
        self.soft_walls = set()
        self.lock = threading.Lock()
        self.connected = False
        self.error = None

    def connect(self, host, port, name):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((host, port))
            sock.settimeout(None)
        except OSError as e:
            self.error = str(e)
            return False

        self.sock = sock
        self.reader = MessageReader(sock)
        self.name = name
        try:
            # sent immediately, ahead of the "welcome" reply - the server
            # blocks briefly waiting for this before it creates our Player,
            # so our name is already in place for the very first roster.
            send_message(sock, {"type": "hello", "name": name})
        except OSError as e:
            self.error = str(e)
            return False

        self.connected = True
        threading.Thread(target=self._listen_loop, daemon=True).start()
        return True

    def _listen_loop(self):
        while self.connected:
            try:
                messages = self.reader.read_messages()
            except OSError:
                break
            if messages is None:
                break
            for msg in messages:
                self._handle_message(msg)
        self.connected = False

    def _handle_message(self, msg):
        msg_type = msg["type"]
        if msg_type == "welcome":
            self.player_id = msg["player_id"]
            self.team = msg["team"]
            self.phase = msg.get("phase", "lobby")
            self.hard_walls = {tuple(cell) for cell in msg["walls"]["hard"]}
            self.soft_walls = {tuple(cell) for cell in msg["walls"]["soft"]}
        elif msg_type == "state":
            now_ms = pygame.time.get_ticks()
            with self.lock:
                self.prev_players = self.players
                self.prev_tick_ms = self.state_tick_ms
                self.players = msg["players"]
                self.state_tick_ms = now_ms
                self.bombs = msg.get("bombs", [])
                self.blasts = msg.get("blasts", [])  # [{"col","row","piece"}, ...]
                self.power_ups = msg.get("power_ups", [])
                self.flags = msg.get("flags", {})
                self.scores = msg.get("scores", {})
                self.winner = msg.get("winner")
                self.phase = msg.get("phase", self.phase)
                # our own team can change in the lobby (host-assigned, or
                # auto-balanced when the host starts) - the "welcome" team is
                # just the initial one
                own = self.players.get(str(self.player_id))
                if own is not None:
                    self.team = own.get("team", self.team)
        elif msg_type == "wall_destroyed":
            cell = tuple(msg["cell"])
            self.soft_walls.discard(cell)
            with self.lock:
                self.recent_destructions.append(cell)
        elif msg_type == "walls_reset":
            # the map regenerated (a "replay" rematch) - full replace, unlike
            # the incremental wall_destroyed above
            self.hard_walls = {tuple(cell) for cell in msg["hard"]}
            self.soft_walls = {tuple(cell) for cell in msg["soft"]}
        elif msg_type == "game_ended":
            self.host_ended = True
            self.connected = False

    def drain_destructions(self):
        with self.lock:
            cells = list(self.recent_destructions)
            self.recent_destructions.clear()
        return cells

    def send_input(self, dx, dy):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "input", "dx": dx, "dy": dy})
        except OSError:
            self.connected = False

    def send_place_bomb(self):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "place_bomb"})
        except OSError:
            self.connected = False

    def send_detonate(self):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "detonate"})
        except OSError:
            self.connected = False

    def send_start_match(self):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "start_match"})
        except OSError:
            self.connected = False

    def send_assign_team(self, target_player_id, team):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "assign_team", "player_id": int(target_player_id), "team": team})
        except OSError:
            self.connected = False

    def send_replay(self):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "replay"})
        except OSError:
            self.connected = False

    def send_change_teams(self):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "change_teams"})
        except OSError:
            self.connected = False

    def send_end_game(self):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "end_game"})
        except OSError:
            self.connected = False

    def render_state(self):
        """Snapshot of everything needed to draw a frame. Player positions are
        interpolated between the last two server updates (30/sec) so 60fps
        rendering doesn't visibly hold-then-jump between ticks.

        Renders RENDER_INTERP_DELAY_MS behind real-time so it's always
        blending between two already-confirmed positions (prev -> curr)
        rather than guessing ahead of the latest one - guessing ahead looks
        smooth until server timing jitters, then visibly snaps back to
        correct itself.
        """
        render_time = pygame.time.get_ticks() - settings.RENDER_INTERP_DELAY_MS
        with self.lock:
            curr, prev = self.players, self.prev_players
            curr_ms, prev_ms = self.state_tick_ms, self.prev_tick_ms
            bombs = list(self.bombs)
            blasts = list(self.blasts)
            power_ups = list(self.power_ups)
            flags = dict(self.flags)
            scores = dict(self.scores)
            winner = self.winner

        span = max(1, curr_ms - prev_ms)
        alpha = min(1.0, max(0.0, (render_time - prev_ms) / span))

        players = {}
        for pid, p in curr.items():
            before = prev.get(pid)
            if before is None:
                players[pid] = p
                continue
            blended = dict(p)
            blended["x"] = before["x"] + (p["x"] - before["x"]) * alpha
            blended["y"] = before["y"] + (p["y"] - before["y"]) * alpha
            players[pid] = blended

        return {
            "players": players,
            "bombs": bombs,
            "blasts": blasts,
            "power_ups": power_ups,
            "flags": flags,
            "scores": scores,
            "winner": winner,
        }


def start_local_server():
    game_server = server_module.GameServer(settings.DEFAULT_PORT)
    threading.Thread(target=game_server.start, daemon=True).start()
    game_server.ready.wait(timeout=3)  # avoid connecting before the socket is listening
    return game_server


def get_local_ip():
    """LAN IP other players on the same network should connect to. Doesn't
    actually send any packets - just asks the OS which local address it
    would route through - so this is instant and works offline.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        probe.close()
        return ip
    except OSError:
        return "unknown"


def fetch_public_ip(host_info):
    """Runs in a background thread - a real network request, so it shouldn't
    block the render loop. Only meaningful once port forwarding is set up.
    """
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as resp:
            host_info["public_ip"] = resp.read().decode().strip()
    except Exception:
        host_info["public_ip"] = None
    host_info["public_status"] = "done"


def draw_flag(screen, x, y, color):
    pole_x = x + settings.CELL_SIZE * 0.35
    top_y = y + settings.CELL_SIZE * 0.12
    bottom_y = y + settings.CELL_SIZE * 0.88
    pygame.draw.line(screen, (225, 225, 225), (pole_x, top_y), (pole_x, bottom_y), 3)
    pygame.draw.polygon(screen, color, [
        (pole_x, top_y),
        (pole_x + settings.CELL_SIZE * 0.4, top_y + settings.CELL_SIZE * 0.14),
        (pole_x, top_y + settings.CELL_SIZE * 0.28),
    ])


def draw_nameplate(screen, small_font, text, center_x, sprite_top_y):
    """Small "name(id)" label centered just above a player's sprite, with a
    translucent backdrop so it stays readable over any background tile.
    """
    text_surf = small_font.render(text, True, (255, 255, 255))
    rect = text_surf.get_rect(midbottom=(center_x, sprite_top_y - 2))
    backdrop = pygame.Surface((rect.width + 6, rect.height + 2), pygame.SRCALPHA)
    backdrop.fill((0, 0, 0, 130))
    screen.blit(backdrop, (rect.x - 3, rect.y - 1))
    screen.blit(text_surf, rect)


def anim_frame(start_ticks, now_ticks, frame_ms, frame_count, loop=True):
    index = (now_ticks - start_ticks) // frame_ms
    return int(index) % frame_count if loop else min(int(index), frame_count - 1)


class Button:
    def __init__(self, rect, label):
        self.rect = pygame.Rect(rect)
        self.label = label

    def draw(self, screen, font):
        pygame.draw.rect(screen, (70, 70, 90), self.rect, border_radius=6)
        pygame.draw.rect(screen, (200, 200, 220), self.rect, width=2, border_radius=6)
        text = font.render(self.label, True, (240, 240, 240))
        screen.blit(text, text.get_rect(center=self.rect.center))

    def clicked(self, pos):
        return self.rect.collidepoint(pos)


def run():
    pygame.init()
    screen = pygame.display.set_mode((settings.FIELD_WIDTH, settings.FIELD_HEIGHT))
    pygame.display.set_caption("Bomberman CTF - Prototype")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("consolas", 24)
    small_font = pygame.font.SysFont("consolas", 18)
    sprites = sprites_module.load_sprites()  # must load after set_mode (needs a display surface)

    state = "menu"
    host_button = Button((300, 240, 200, 50), "Host Game")
    join_button = Button((300, 310, 200, 50), "Join Game")
    # centered under the Red/Blue roster columns drawn in the lobby - the
    # lobby owner selects a player (click their row), then one of these to
    # place them on that team
    assign_red_button = Button((97, 400, 180, 45), "Assign -> Red")
    assign_blue_button = Button((573, 400, 180, 45), "Assign -> Blue")
    start_match_button = Button((300, 480, 200, 50), "Start Game")

    finished_button_y = settings.FIELD_HEIGHT // 2 + 50
    replay_button = Button((135, finished_button_y, 180, 50), "Replay")
    change_teams_button = Button((355, finished_button_y, 180, 50), "Change Teams")
    back_menu_button = Button((575, finished_button_y, 180, 50), "Main Menu")

    name_text = "Player"
    join_text = ""
    status_message = ""
    selected_pid = None  # lobby: the player row the owner clicked, awaiting an Assign click
    lobby_rows = []  # [(pid, rect), ...] - rebuilt each lobby draw, hit-tested against on the next frame's click

    net = NetworkClient()
    local_server = None  # the GameServer this client is hosting, if any - so "Main Menu" can free the port
    last_sent = (0, 0)
    host_info = {"local_ip": None, "public_ip": None, "public_status": "not_started"}

    def state_for_phase(phase):
        return {"lobby": "lobby", "playing": "game", "finished": "finished"}.get(phase, "lobby")

    # client-side-only animation bookkeeping - none of this is networked state
    destroy_anim = {}  # (col, row) -> start_ticks, brick crumble one-shot
    blast_anim = {}  # (col, row) -> start_ticks, explosion frame timing
    death_anim = {}  # player_id -> start_ticks, death one-shot (triggered by a position-jump heuristic)
    last_positions = {}  # player_id -> (x, y), to detect the jump that means "this player respawned"

    running = True
    while running:
        clock.tick(60)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif state == "menu" and event.type == pygame.MOUSEBUTTONDOWN:
                if host_button.clicked(event.pos):
                    display_name = name_text.strip() or "Player"
                    local_server = start_local_server()
                    if net.connect("127.0.0.1", settings.DEFAULT_PORT, display_name):
                        state = state_for_phase(net.phase)
                        host_info["local_ip"] = get_local_ip()
                        host_info["public_status"] = "fetching"
                        threading.Thread(target=fetch_public_ip, args=(host_info,), daemon=True).start()
                    else:
                        status_message = f"Failed to connect: {net.error}"
                elif join_button.clicked(event.pos):
                    state = "join_input"
                    join_text = ""

            elif state == "menu" and event.type == pygame.KEYDOWN:
                if event.key == pygame.K_BACKSPACE:
                    name_text = name_text[:-1]
                elif event.unicode.isprintable() and len(name_text) < settings.MAX_NAME_LENGTH:
                    name_text += event.unicode

            elif state == "join_input" and event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    state = "menu"
                elif event.key == pygame.K_RETURN:
                    display_name = name_text.strip() or "Player"
                    if net.connect(join_text.strip(), settings.DEFAULT_PORT, display_name):
                        state = state_for_phase(net.phase)
                    else:
                        status_message = f"Failed to connect: {net.error}"
                        state = "menu"
                elif event.key == pygame.K_BACKSPACE:
                    join_text = join_text[:-1]
                elif event.unicode.isprintable():
                    join_text += event.unicode

            elif state == "lobby" and event.type == pygame.MOUSEBUTTONDOWN and net.player_id == 1:
                if start_match_button.clicked(event.pos):
                    net.send_start_match()
                elif assign_red_button.clicked(event.pos):
                    if selected_pid is not None:
                        net.send_assign_team(selected_pid, 0)
                elif assign_blue_button.clicked(event.pos):
                    if selected_pid is not None:
                        net.send_assign_team(selected_pid, 1)
                else:
                    for pid, rect in lobby_rows:
                        if rect.collidepoint(event.pos):
                            selected_pid = pid
                            break

            elif state == "finished" and event.type == pygame.MOUSEBUTTONDOWN and net.player_id == 1:
                if replay_button.clicked(event.pos):
                    net.send_replay()
                elif change_teams_button.clicked(event.pos):
                    net.send_change_teams()
                elif back_menu_button.clicked(event.pos):
                    net.send_end_game()
                    if local_server is not None:
                        local_server.shutdown()  # belt-and-suspenders in case the round-trip lags
                        local_server = None
                    net = NetworkClient()
                    selected_pid = None
                    status_message = ""
                    state = "menu"

            elif state == "game" and event.type == pygame.KEYDOWN:
                if event.key == pygame.K_SPACE:
                    net.send_place_bomb()
                elif event.key == pygame.K_e:
                    net.send_detonate()

        if state in ("lobby", "game", "finished") and not net.connected:
            status_message = "Host ended the game." if net.host_ended else "Disconnected from server."
            state = "menu"

        elif state == "lobby":
            if net.phase == "playing":
                state = "game"

        elif state == "game":
            if net.phase == "finished":
                state = "finished"
            else:
                keys = pygame.key.get_pressed()
                dx = (keys[pygame.K_d] or keys[pygame.K_RIGHT]) - (keys[pygame.K_a] or keys[pygame.K_LEFT])
                dy = (keys[pygame.K_s] or keys[pygame.K_DOWN]) - (keys[pygame.K_w] or keys[pygame.K_UP])
                if (dx, dy) != last_sent:
                    net.send_input(dx, dy)
                    last_sent = (dx, dy)

        elif state == "finished":
            if net.phase == "lobby":
                selected_pid = None
                state = "lobby"
            elif net.phase == "playing":
                state = "game"

        screen.fill((30, 30, 40))

        if state == "menu":
            title = font.render("Bomberman CTF", True, (240, 240, 240))
            screen.blit(title, title.get_rect(center=(settings.FIELD_WIDTH // 2, 120)))

            name_label = small_font.render("Your name:", True, (200, 200, 200))
            screen.blit(name_label, name_label.get_rect(center=(settings.FIELD_WIDTH // 2, 165)))
            name_box = pygame.Rect(250, 183, 300, 40)
            pygame.draw.rect(screen, (50, 50, 65), name_box)
            pygame.draw.rect(screen, (200, 200, 220), name_box, width=2)
            name_surf = font.render(name_text, True, (240, 240, 240))
            screen.blit(name_surf, (name_box.x + 8, name_box.y + 8))

            host_button.draw(screen, font)
            join_button.draw(screen, font)
            if status_message:
                msg = small_font.render(status_message, True, (255, 150, 150))
                screen.blit(msg, msg.get_rect(center=(settings.FIELD_WIDTH // 2, 400)))

        elif state == "join_input":
            prompt = font.render("Enter host IP, then press Enter:", True, (240, 240, 240))
            screen.blit(prompt, prompt.get_rect(center=(settings.FIELD_WIDTH // 2, 220)))
            box = pygame.Rect(250, 260, 300, 40)
            pygame.draw.rect(screen, (50, 50, 65), box)
            pygame.draw.rect(screen, (200, 200, 220), box, width=2)
            text_surf = font.render(join_text, True, (240, 240, 240))
            screen.blit(text_surf, (box.x + 8, box.y + 8))
            hint = small_font.render("Esc to cancel  |  127.0.0.1 for same-PC testing", True, (180, 180, 180))
            screen.blit(hint, hint.get_rect(center=(settings.FIELD_WIDTH // 2, 320)))

        elif state == "lobby":
            title = font.render("Lobby", True, (240, 240, 240))
            screen.blit(title, title.get_rect(center=(settings.FIELD_WIDTH // 2, 100)))

            if net.player_id == 1:
                local_ip = host_info["local_ip"] or "..."
                local_line = small_font.render(
                    f"LAN: {local_ip}:{settings.DEFAULT_PORT}  (same wifi/network)", True, (180, 210, 255)
                )
                screen.blit(local_line, local_line.get_rect(center=(settings.FIELD_WIDTH // 2, 128)))

                if host_info["public_status"] == "fetching":
                    public_text = "Internet: looking up public IP..."
                elif host_info["public_ip"]:
                    public_text = f"Internet: {host_info['public_ip']}:{settings.DEFAULT_PORT}  (needs port forwarding set up)"
                else:
                    public_text = "Internet: unavailable (no connection?)"
                public_line = small_font.render(public_text, True, (170, 170, 170))
                screen.blit(public_line, public_line.get_rect(center=(settings.FIELD_WIDTH // 2, 150)))

            roster = net.render_state()["players"]
            header_y = 180
            list_top = 205
            is_owner = net.player_id == 1

            # three columns: each team's roster either side, players the
            # owner hasn't assigned yet in the middle
            columns = [
                (0, settings.FIELD_WIDTH * 0.22, f"Team {settings.TEAM_NAMES[0]}", settings.TEAM_COLORS[0]),
                (None, settings.FIELD_WIDTH * 0.5, "Unassigned", settings.UNASSIGNED_COLOR),
                (1, settings.FIELD_WIDTH * 0.78, f"Team {settings.TEAM_NAMES[1]}", settings.TEAM_COLORS[1]),
            ]
            for _, x, label, color in columns:
                header = small_font.render(label, True, color)
                screen.blit(header, header.get_rect(center=(x, header_y)))

            if not roster:
                waiting = small_font.render("Waiting for players to connect...", True, (180, 180, 180))
                screen.blit(waiting, waiting.get_rect(center=(settings.FIELD_WIDTH // 2, list_top)))

            lobby_rows = []
            sorted_roster = sorted(roster.items(), key=lambda kv: int(kv[0]))
            for team_key, x, _, color in columns:
                members = [(pid, p) for pid, p in sorted_roster if p.get("team") == team_key]
                for i, (pid, p) in enumerate(members):
                    display_name = p.get("name") or f"Player{pid}"
                    you_tag = "  (you)" if str(pid) == str(net.player_id) else ""
                    host_tag = "  [HOST]" if pid == "1" else ""
                    row_y = list_top + i * 24
                    line = small_font.render(f"{display_name}({pid}){host_tag}{you_tag}", True, color)
                    line_rect = line.get_rect(center=(x, row_y))

                    if is_owner:
                        click_rect = line_rect.inflate(16, 6)
                        lobby_rows.append((pid, click_rect))
                        if pid == selected_pid:
                            pygame.draw.rect(screen, (90, 90, 120), click_rect, border_radius=4)
                            pygame.draw.rect(screen, (230, 230, 240), click_rect, width=1, border_radius=4)

                    screen.blit(line, line_rect)

            if is_owner:
                assign_red_button.draw(screen, font)
                assign_blue_button.draw(screen, font)
                hint_text = (
                    f"Selected: {selected_pid}  -  click Assign" if selected_pid is not None
                    else "Click a player, then Assign -> Red/Blue"
                )
                hint = small_font.render(hint_text, True, (180, 180, 190))
                screen.blit(hint, hint.get_rect(center=(settings.FIELD_WIDTH // 2, 460)))
                start_match_button.draw(screen, font)
            else:
                waiting = small_font.render("Waiting for the host to assign teams and start...", True, (200, 200, 200))
                screen.blit(waiting, waiting.get_rect(center=(settings.FIELD_WIDTH // 2, 505)))

        elif state in ("game", "finished"):
            now_ticks = pygame.time.get_ticks()

            for col in range(settings.GRID_COLS):
                for row in range(settings.GRID_ROWS):
                    screen.blit(sprites.ground, (col * settings.CELL_SIZE, row * settings.CELL_SIZE))

            all_walls = net.hard_walls | net.soft_walls
            for col, row in all_walls:
                shadow_row = row + 1
                if shadow_row < settings.GRID_ROWS and (col, shadow_row) not in all_walls:
                    screen.blit(sprites.ground_shadow, (col * settings.CELL_SIZE, shadow_row * settings.CELL_SIZE))

            pygame.draw.rect(screen, (20, 20, 25), (0, 0, settings.FIELD_WIDTH, settings.FIELD_HEIGHT), width=3)

            for col, row in net.hard_walls:
                screen.blit(sprites.block, (col * settings.CELL_SIZE, row * settings.CELL_SIZE))

            for col, row in net.soft_walls:
                screen.blit(sprites.brick, (col * settings.CELL_SIZE, row * settings.CELL_SIZE))

            for cell in net.drain_destructions():
                destroy_anim[cell] = now_ticks
            for cell, start in list(destroy_anim.items()):
                frame = anim_frame(start, now_ticks, settings.DESTROY_FRAME_MS, len(sprites.brick_destroy), loop=False)
                screen.blit(sprites.brick_destroy[frame], (cell[0] * settings.CELL_SIZE, cell[1] * settings.CELL_SIZE))
                if now_ticks - start > settings.DESTROY_FRAME_MS * len(sprites.brick_destroy):
                    destroy_anim.pop(cell, None)

            render_data = net.render_state()
            snapshot = render_data["players"]
            bombs = render_data["bombs"]
            blasts = render_data["blasts"]
            power_ups = render_data["power_ups"]
            flags = render_data["flags"]
            scores = render_data["scores"]
            winner = render_data["winner"]

            item_sprites = {
                "bomb": sprites.item_bomb, "fire": sprites.item_fire, "speed": sprites.item_speed,
                "disease": sprites.item_disease, "timer": sprites.item_timer, "kick": sprites.item_kick,
            }
            for power_up in power_ups:
                kind = power_up["kind"]
                pos = (power_up["col"] * settings.CELL_SIZE, power_up["row"] * settings.CELL_SIZE)
                if kind in item_sprites:
                    screen.blit(item_sprites[kind], pos)
                else:
                    center = (pos[0] + settings.CELL_SIZE // 2, pos[1] + settings.CELL_SIZE // 2)
                    color = settings.POWER_UP_COLORS[kind]
                    pygame.draw.circle(screen, color, center, int(settings.CELL_SIZE * 0.3))
                    pygame.draw.circle(screen, (255, 255, 255), center, int(settings.CELL_SIZE * 0.3), width=2)
                    label = small_font.render(settings.POWER_UP_LABELS[kind], True, (20, 20, 20))
                    screen.blit(label, label.get_rect(center=center))

            bomb_frame = anim_frame(0, now_ticks, settings.BOMB_FRAME_MS, len(sprites.bomb))
            for bomb in bombs:
                screen.blit(sprites.bomb[bomb_frame], (bomb["x"], bomb["y"]))

            seen_blast_cells = set()
            for blast in blasts:
                cell = (blast["col"], blast["row"])
                seen_blast_cells.add(cell)
                start = blast_anim.setdefault(cell, now_ticks)
                frame = anim_frame(start, now_ticks, settings.EXPLOSION_FRAME_MS, 8, loop=False)
                pos = (cell[0] * settings.CELL_SIZE, cell[1] * settings.CELL_SIZE)
                piece = blast["piece"]
                if piece == "start":
                    screen.blit(sprites.explosion_start[frame], pos)
                elif piece in ("mid_h", "mid_v"):
                    screen.blit(sprites.explosion_middle[piece][frame], pos)
                else:
                    screen.blit(sprites.explosion_end[piece][frame], pos)
            for cell in list(blast_anim.keys()):
                if cell not in seen_blast_cells:
                    blast_anim.pop(cell, None)

            for team_key, flag in flags.items():
                team = int(team_key)
                draw_flag(screen, flag["x"], flag["y"], settings.TEAM_COLORS[team])

            highlight = pygame.Surface((settings.CELL_SIZE, settings.CELL_SIZE), pygame.SRCALPHA)
            for p in snapshot.values():
                color = tuple(p["color"])
                for cell_col, cell_row in p.get("cells", []):
                    highlight.fill((*color, 60))
                    screen.blit(highlight, (cell_col * settings.CELL_SIZE, cell_row * settings.CELL_SIZE))

            blink_phase = (now_ticks // 150) % 4  # color -> white -> color -> black -> repeat
            sprite_h = sprites.player_walk[0]["down"][0].get_height()
            y_offset = settings.CELL_SIZE - sprite_h  # anchors feet to the tile's bottom edge

            for pid, p in snapshot.items():
                team = p.get("team", 0)
                prev_pos = last_positions.get(pid, (p["x"], p["y"]))
                jumped = abs(p["x"] - prev_pos[0]) + abs(p["y"] - prev_pos[1]) > settings.DEATH_JUMP_THRESHOLD
                last_positions[pid] = (p["x"], p["y"])
                if jumped:
                    death_anim[pid] = now_ticks

                pos = (p["x"], p["y"] + y_offset)

                if pid in death_anim:
                    frames = sprites.player_death[team]
                    elapsed = now_ticks - death_anim[pid]
                    frame = min(int(elapsed // settings.DEATH_FRAME_MS), len(frames) - 1)
                    screen.blit(frames[frame], pos)
                    if elapsed > settings.DEATH_FRAME_MS * len(frames):
                        death_anim.pop(pid, None)
                    continue

                direction_frames = sprites.player_walk[team][p.get("facing", "down")]
                if p.get("is_moving"):
                    # scale frame duration by actual speed (relative to the configured
                    # base speed) so the walk cycle always takes roughly one cell
                    # crossing to complete a loop, whether at normal speed, slowed
                    # down for testing, superspeed, flag-carry, etc. - a fixed frame
                    # duration would desync from a much slower or faster crossing and
                    # look like it's repeating/stuck even though position is moving fine
                    speed_ratio = max(0.1, p.get("cells_per_sec", settings.GRID_MOVE_SPEED)) / settings.GRID_MOVE_SPEED
                    frame_ms = max(20, round(settings.WALK_FRAME_MS / speed_ratio))
                    frame = anim_frame(0, now_ticks, frame_ms, len(direction_frames))
                else:
                    frame = 0
                sprite = direction_frames[frame]
                if p.get("disease") and blink_phase in (1, 3):
                    sprite = sprite.copy()
                    if blink_phase == 1:
                        sprite.fill((255, 255, 255), special_flags=pygame.BLEND_RGB_ADD)
                    else:
                        sprite.fill((255, 255, 255), special_flags=pygame.BLEND_RGB_SUB)
                screen.blit(sprite, pos)
                if str(pid) == str(net.player_id):
                    pygame.draw.rect(screen, (255, 255, 255), (*pos, sprite.get_width(), sprite.get_height()), width=2)

                nameplate = p.get("name") or f"Player{pid}"
                draw_nameplate(screen, small_font, f"{nameplate}({pid})", pos[0] + sprite.get_width() // 2, pos[1])

            team_name = settings.TEAM_NAMES[net.team] if net.team is not None else "?"
            own_name = net.name or f"Player{net.player_id}"
            hud = small_font.render(
                f"{own_name}({net.player_id})  Team {team_name}  |  Space: bomb  |  E: detonate", True, (220, 220, 220)
            )
            screen.blit(hud, (10, 10))

            own = snapshot.get(str(net.player_id))
            if own:
                stats_text = f"Bombs: {own['bomb_capacity']}   Range: {own['blast_range']}   Speed: +{own['speed_bonus']:.1f}"
                if own.get("has_kick"):
                    stats_text += "   KICK"
                if own.get("has_remote"):
                    stats_text += "   REMOTE"
                stats = small_font.render(stats_text, True, (200, 220, 200))
                screen.blit(stats, (10, 32))

            score_text = "   ".join(
                f"{settings.TEAM_NAMES[int(team_key)]}: {score}" for team_key, score in scores.items()
            )
            score_hud = font.render(score_text, True, (240, 240, 240))
            screen.blit(score_hud, score_hud.get_rect(midtop=(settings.FIELD_WIDTH // 2, 8)))

            if winner is not None:
                banner = font.render(f"{settings.TEAM_NAMES[winner]} TEAM WINS!", True, (255, 230, 120))
                banner_rect = banner.get_rect(center=(settings.FIELD_WIDTH // 2, settings.FIELD_HEIGHT // 2))

                if state == "finished":
                    panel = pygame.Rect(0, 0, 460, 150)
                    panel.center = (settings.FIELD_WIDTH // 2, settings.FIELD_HEIGHT // 2 + 25)
                    overlay = pygame.Surface(panel.size, pygame.SRCALPHA)
                    overlay.fill((15, 15, 20, 210))
                    screen.blit(overlay, panel)
                    pygame.draw.rect(screen, (200, 200, 220), panel, width=2, border_radius=8)
                else:
                    pygame.draw.rect(screen, (20, 20, 25), banner_rect.inflate(24, 16))

                screen.blit(banner, banner_rect)

                if state == "finished":
                    if net.player_id == 1:
                        replay_button.draw(screen, font)
                        change_teams_button.draw(screen, font)
                        back_menu_button.draw(screen, font)
                    else:
                        waiting = small_font.render(
                            "Waiting for the host to choose what's next...", True, (200, 200, 200)
                        )
                        screen.blit(waiting, waiting.get_rect(
                            center=(settings.FIELD_WIDTH // 2, finished_button_y + 25)
                        ))

        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    run()

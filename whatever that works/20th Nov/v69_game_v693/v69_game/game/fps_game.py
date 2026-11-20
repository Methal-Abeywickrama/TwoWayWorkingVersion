"""
fps_game.py - Radio Arena v3: multi-player (2-8) 3D arena shooter over the CDP radio, host-authoritative.
Runs on the v6.9 cdp_link (socket bridge, default) or on the v6.8 flowgraph's Game Link block (--stack v68).

Main menu: name, packet rate, max players, lag limit, host election (lowest ping / random), ARQ on / bypass.
Lobby: every node that runs the game, with status, link (two-way / one-way), ping. "Enter arena" joins the
running arena or elects a host. In the arena the host judges every shot (rewinding the other players to what
the shooter saw) and owns HP, kills and respawns; see game_core.py.

Controls: WASD move | mouse look (arrow keys also turn) | Left mouse shoot (hold = automatic) | R reload
          Space jump | Shift sprint | TAB scoreboard | Esc pause menu | F10 quit

Run:
    v6.9: "Start game" in cdp_daemon.py (runs cdp_game.py with CDP_MY_ID, CDP_PEERS, ports, ...)
    v6.8: started by the flowgraph (Game Link v2 block, GAME_LAUNCH = 1) with --stack v68
    python3 game/fps_game.py                      # menu; this node = $CDP_MY_ID (or --node N)
    python3 game/fps_game.py --practice 3         # offline vs 3 bots at once
Needs: pygame (or pygame-ce) and PyOpenGL. No GLU needed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from typing import Optional

import pygame
from pygame.locals import DOUBLEBUF, OPENGL


def _match_gl_loader():
    """Make PyOpenGL load the same GL loader that SDL uses for the window, BEFORE OpenGL is imported.

    On a Wayland desktop PyOpenGL picks EGL (from XDG_SESSION_TYPE=wayland). If SDL opens the window
    through X11/XWayland it creates a GLX context instead, and then every GL call fails, so the game
    quit right after starting. Here SDL is asked which backend it uses: wayland -> EGL, x11 -> GLX.
    PYOPENGL_PLATFORM set by hand always wins."""
    if "--x11" in sys.argv[1:]:
        os.environ["SDL_VIDEODRIVER"] = "x11"
    if os.environ.get("PYOPENGL_PLATFORM"):
        return
    try:
        pygame.display.init()
        drv = (pygame.display.get_driver() or "").lower()
    except Exception:
        return
    if drv == "wayland":
        os.environ["PYOPENGL_PLATFORM"] = "egl"
    elif drv == "x11":
        os.environ["PYOPENGL_PLATFORM"] = "linux"            # GLX


_match_gl_loader()
from OpenGL.GL import *          # noqa: E402,F401,F403

from game_core import (BODY_TOP, CELL, COLS, DEFAULT_MAX_LAG, DEFAULT_MAX_PLAYERS, DEFAULT_RATE, EYE_H,  # noqa: E402
                       HEAL_COOLDOWN, HEAL_TIME, INF, MAG, MAP, MAX_HP, MAX_PLAYERS_LIMIT, MAX_RATE, MEDKIT_MAX,
                       MIN_RATE, RELOAD_TIME, ROWS, SWITCH_TIME, WALL_H, WEAPONS, BridgeLink, Node, Practice, RadioLink,
                       cell_info, floor_at, line_of_sight, look_dir, ray_top_at, solid_at, solid_cell, wall_hit)
from payload import W_KNIFE, W_RIFLE, W_SNIPER                                                          # noqa: E402

# ------------------------------------------------------------------ look & feel
SCOPE_FOV = 20.0                                   # sniper scope (right mouse)
SUN_DIR = (-0.42, 0.62, -0.66)                     # direction *towards* the sun
_l = math.sqrt(sum(c * c for c in SUN_DIR))
SUN_DIR = tuple(c / _l for c in SUN_DIR)
SUN_COL = (1.00, 0.93, 0.80)
AMB_COL = (0.46, 0.52, 0.64)                       # sky light, a bit blue
SKY_ZENITH = (0.22, 0.42, 0.78)
SKY_HORIZON = (0.72, 0.80, 0.90)
FOG_START, FOG_END = 22.0, 75.0
PALETTE = [(0.18, 0.42, 0.92), (0.90, 0.24, 0.20), (0.20, 0.72, 0.30), (0.95, 0.68, 0.12),
           (0.62, 0.30, 0.85), (0.12, 0.72, 0.78), (0.95, 0.42, 0.68), (0.60, 0.60, 0.60)]
TEX = 256                                          # texture size (px), covers one 2 m cell


# ------------------------------------------------------------------ procedural textures
def _value_noise(size, cells, rng):
    """Tileable smooth value noise in 0..1, returned as a flat list."""
    g = [[rng.random() for _ in range(cells)] for _ in range(cells)]
    out = [0.0] * (size * size)
    step = size / cells
    for y in range(size):
        fy = y / step
        y0 = int(fy) % cells
        y1 = (y0 + 1) % cells
        ty = fy - int(fy)
        ty = ty * ty * (3 - 2 * ty)
        row0, row1 = g[y0], g[y1]
        base = y * size
        for x in range(size):
            fx = x / step
            x0 = int(fx) % cells
            x1 = (x0 + 1) % cells
            tx = fx - int(fx)
            tx = tx * tx * (3 - 2 * tx)
            a = row0[x0] + (row0[x1] - row0[x0]) * tx
            b = row1[x0] + (row1[x1] - row1[x0]) * tx
            out[base + x] = a + (b - a) * ty
    return out


def _clamp8(v):
    return 0 if v < 0 else 255 if v > 255 else int(v)


def make_brick_texture(rng):
    """Concrete blocks 50 x 25 cm, staggered, with mortar. 256 px = 2 m."""
    n1 = _value_noise(TEX, 16, rng)
    n2 = _value_noise(TEX, 64, rng)
    bw, bh, mortar = 64, 32, 3
    tints = {}
    data = bytearray(TEX * TEX * 3)
    for y in range(TEX):
        row = y // bh
        off = (bw // 2) if row % 2 else 0
        yy = y % bh
        for x in range(TEX):
            xs = (x + off) % TEX
            col = xs // bw
            xx = xs % bw
            i = y * TEX + x
            grain = (n1[i] - 0.5) * 0.25 + (n2[i] - 0.5) * 0.18 + (rng.random() - 0.5) * 0.06
            if xx < mortar or yy < mortar:
                r, g, b = 0.56, 0.54, 0.50
                shade = 0.88 + grain
            else:
                key = (row, col)
                if key not in tints:
                    tints[key] = (rng.uniform(-0.06, 0.06), rng.uniform(-0.03, 0.03))
                t, w = tints[key]
                r, g, b = 0.80 + t + w, 0.74 + t, 0.63 + t - w
                edge = min(xx - mortar, yy - mortar, bw - 1 - xx, bh - 1 - yy)
                bevel = 0.92 if edge < 2 else 1.0
                shade = (1.0 + grain) * bevel
            o = i * 3
            data[o] = _clamp8(r * shade * 255)
            data[o + 1] = _clamp8(g * shade * 255)
            data[o + 2] = _clamp8(b * shade * 255)
    return bytes(data)


def make_floor_texture(rng):
    """Concrete slabs 1 m with dark joints and stains. 256 px = 2 m."""
    n1 = _value_noise(TEX, 8, rng)
    n2 = _value_noise(TEX, 32, rng)
    n3 = _value_noise(TEX, 128, rng)
    tile, joint = 128, 2
    tints = {(a, b): rng.uniform(-0.05, 0.05) for a in range(2) for b in range(2)}
    data = bytearray(TEX * TEX * 3)
    for y in range(TEX):
        for x in range(TEX):
            i = y * TEX + x
            stain = (n1[i] - 0.5) * 0.30
            grain = (n2[i] - 0.5) * 0.14 + (n3[i] - 0.5) * 0.10 + (rng.random() - 0.5) * 0.05
            if x % tile < joint or y % tile < joint:
                shade = 0.45 + grain * 0.5
            else:
                shade = 1.0 + stain + grain + tints[(y // tile, x // tile)]
            o = i * 3
            data[o] = _clamp8(0.60 * shade * 255)
            data[o + 1] = _clamp8(0.61 * shade * 255)
            data[o + 2] = _clamp8(0.58 * shade * 255)
    return bytes(data)


def make_cloud_texture(rng, size=128):
    """Tileable soft clouds, RGBA (white with alpha)."""
    n1 = _value_noise(size, 4, rng)
    n2 = _value_noise(size, 8, rng)
    n3 = _value_noise(size, 16, rng)
    data = bytearray(size * size * 4)
    for i in range(size * size):
        v = n1[i] * 0.55 + n2[i] * 0.30 + n3[i] * 0.15
        a = max(0.0, min(1.0, (v - 0.48) * 3.2))
        o = i * 4
        shade = 235 + int(20 * (v - 0.5))
        data[o] = data[o + 1] = _clamp8(shade)
        data[o + 2] = _clamp8(shade + 8)
        data[o + 3] = _clamp8(a * 255)
    return bytes(data), size


def upload_texture(data, size, fmt, aniso=True):
    tid = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, tid)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_GENERATE_MIPMAP, GL_TRUE)
    if aniso:
        try:
            glTexParameterf(GL_TEXTURE_2D, 0x84FE, 8.0)       # GL_TEXTURE_MAX_ANISOTROPY_EXT
        except Exception:
            pass
    glTexImage2D(GL_TEXTURE_2D, 0, fmt, size, size, 0, fmt, GL_UNSIGNED_BYTE, data)
    return tid


def make_crate_texture(rng):
    """Wooden crate: planks with a dark frame. 256 px = one 2 m face."""
    n1 = _value_noise(TEX, 8, rng)
    n2 = _value_noise(TEX, 64, rng)
    data = bytearray(TEX * TEX * 3)
    plank, frame = 32, 14
    tints = [rng.uniform(-0.07, 0.07) for _ in range(TEX // plank + 1)]
    for y in range(TEX):
        for x in range(TEX):
            i = y * TEX + x
            grain = (n1[i] - 0.5) * 0.18 + (n2[i] - 0.5) * 0.10 + 0.05 * math.sin(x * 0.35 + n1[i] * 9.0)
            edge = min(x, y, TEX - 1 - x, TEX - 1 - y)
            diag = abs(x - y) < 9 or abs(x - (TEX - 1 - y)) < 9
            if edge < frame or diag:
                r, g, b, shade = 0.47, 0.33, 0.19, 0.92 + grain
            else:
                r, g, b = 0.62, 0.46, 0.28
                shade = 1.0 + grain + tints[y // plank] - (0.25 if y % plank < 2 else 0.0)
            o = i * 3
            data[o] = _clamp8(r * shade * 255)
            data[o + 1] = _clamp8(g * shade * 255)
            data[o + 2] = _clamp8(b * shade * 255)
    return bytes(data)


# ------------------------------------------------------------------ baked lighting
def in_sun_shadow(x, y, z):
    """March from a point towards the sun; True if a wall, block or stair is in the way."""
    lx, ly, lz = SUN_DIR
    t = 0.05
    while True:
        py = y + ly * t
        if py >= WALL_H:
            return False
        if py < ray_top_at(x + lx * t, z + lz * t) - 1e-6:
            return True
        t += 0.08


def occluder_distance(x, z, y):
    """Horizontal distance from a point at height y to the nearest wall / block standing higher (5x5 cells)."""
    c0, r0 = int(x // CELL), int(z // CELL)
    best = 9.0
    for r in range(r0 - 2, r0 + 3):
        for c in range(c0 - 2, c0 + 3):
            info = cell_info(r, c)
            top = WALL_H if info[0] == "wall" else info[1] if info[0] == "flat" else max(info[2], info[3])
            if top > y + 0.25:
                dx = max(c * CELL - x, 0.0, x - (c + 1) * CELL)
                dz = max(r * CELL - z, 0.0, z - (r + 1) * CELL)
                best = min(best, math.hypot(dx, dz))
    return best


def light(nx, ny, nz, lit, ao=1.0):
    """Vertex colour multiplier: sky ambient (with AO) + sun (if not shadowed)."""
    ndl = max(0.0, nx * SUN_DIR[0] + ny * SUN_DIR[1] + nz * SUN_DIR[2]) if lit else 0.0
    return tuple(min(1.6, AMB_COL[i] * ao + SUN_COL[i] * ndl * 0.85) for i in range(3))


STAIR_STEPS = 6                       # drawn steps per stair cell (the walking surface is the smooth slope)


def _is_crate(info):
    return info[0] == "flat" and 0 < info[1] <= 1.0


def _edge_top(r, c, side):
    """Lowest top of cell (r, c) along its side facing side=(dr, dc) (walls WALL_H)."""
    info = cell_info(r, c)
    if info[0] == "wall":
        return WALL_H
    if info[0] == "flat":
        return info[1]
    half = abs(info[3] - info[2]) / (2 * STAIR_STEPS)        # drawn treads sit half a step below the slope ends
    if (info[1] == "x") == (side[1] == 0):
        return max(0.0, min(info[2], info[3]) - half)
    return max(0.0, (info[2] if (side[1] < 0 or side[0] < 0) else info[3]) - half)


def build_world_list(tex_wall, tex_floor, tex_crate):
    lst = glGenLists(1)
    glNewList(lst, GL_COMPILE)
    glEnable(GL_TEXTURE_2D)
    cache = {}

    def shade(vx, vy, vz, n, ao_floor=True):
        key = (round(vx, 3), round(vy, 3), round(vz, 3), n)
        if key not in cache:
            lit = not in_sun_shadow(vx + n[0] * 0.05, vy + n[1] * 0.02, vz + n[2] * 0.05)
            if n[1] > 0.5:
                ao = 1.0 - 0.45 * math.exp(-occluder_distance(vx, vz, vy) / 0.35)
            else:
                g = ray_top_at(vx + n[0] * 0.3, vz + n[2] * 0.3)          # darker near the ground in front
                ao = 0.62 + 0.38 * min(1.0, max(0.0, vy - g) / 0.9) ** 0.7 if ao_floor else 0.9
            cache[key] = light(n[0], n[1], n[2], lit, ao)
        return cache[key]

    def quad_h(x0, z0, x1, z1, y, sub, tex_scale=1.0):
        """Horizontal quad (facing up) split into sub x sub."""
        sx, sz = (x1 - x0) / sub, (z1 - z0) / sub
        for i in range(sub):
            for j in range(sub):
                a0, b0 = x0 + j * sx, z0 + i * sz
                for vx, vz in ((a0, b0), (a0, b0 + sz), (a0 + sx, b0 + sz), (a0 + sx, b0)):
                    glColor3f(*shade(vx, y, vz, (0, 1, 0)))
                    glTexCoord2f(vx / CELL * tex_scale, vz / CELL * tex_scale)
                    glVertex3f(vx, y, vz)

    def quad_v(p0, p1, y0, y1, n, hs=None, tex_v=CELL, ao_floor=True):
        """Vertical face from point p0 to p1 (x, z) between heights y0..y1, wound counter-clockwise seen from n."""
        hs = hs or [y0, y1]
        hs = [y0] + [h for h in hs if y0 < h < y1] + [y1]
        ws = max(1, int(round(math.hypot(p1[0] - p0[0], p1[1] - p0[1]) / 0.5)))
        for k in range(ws):
            u0, u1 = k / ws, (k + 1) / ws
            for h in range(len(hs) - 1):
                for u, y in ((u0, hs[h]), (u1, hs[h]), (u1, hs[h + 1]), (u0, hs[h + 1])):
                    wx, wz = p0[0] + (p1[0] - p0[0]) * u, p0[1] + (p1[1] - p0[1]) * u
                    glColor3f(*shade(wx, y, wz, n, ao_floor))
                    glTexCoord2f((wx + wz) / CELL, y / tex_v)
                    glVertex3f(wx, y, wz)

    def faces_of(r, c):
        """The 4 sides of a cell: (neighbour offset, normal, from-point, to-point) wound CCW from outside."""
        x0, z0, x1, z1 = c * CELL, r * CELL, (c + 1) * CELL, (r + 1) * CELL
        return (((-1, 0), (0, 0, -1), (x1, z0), (x0, z0)), ((1, 0), (0, 0, 1), (x0, z1), (x1, z1)),
                ((0, -1), (-1, 0, 0), (x0, z0), (x0, z1)), ((0, 1), (1, 0, 0), (x1, z1), (x1, z0)))

    # ---- ground and the tops of buildings / stairs-free blocks
    for tex, want in ((tex_floor, lambda i: not _is_crate(i)), (tex_crate, _is_crate)):
        glBindTexture(GL_TEXTURE_2D, tex)
        glBegin(GL_QUADS)
        for r in range(ROWS):
            for c in range(COLS):
                info = cell_info(r, c)
                if info[0] != "flat" or not want(info):
                    continue
                quad_h(c * CELL, r * CELL, (c + 1) * CELL, (r + 1) * CELL, info[1], 4 if info[1] > 0 else 6,
                       0.5 if _is_crate(info) else 1.0)
        glEnd()

    # ---- sides of buildings and crates (down to whatever is next to them)
    for tex, want in ((tex_wall, lambda i: not _is_crate(i)), (tex_crate, _is_crate)):
        glBindTexture(GL_TEXTURE_2D, tex)
        glBegin(GL_QUADS)
        for r in range(ROWS):
            for c in range(COLS):
                info = cell_info(r, c)
                if info[0] != "flat" or info[1] <= 0 or not want(info):
                    continue
                for (dr, dc), n, p0, p1 in faces_of(r, c):
                    below = _edge_top(r + dr, c + dc, (-dr, -dc))
                    if below < info[1]:
                        quad_v(p0, p1, max(0.0, min(below, info[1])), info[1], n,
                               [0.25, 0.6, 1.2, 1.8, 2.4, 3.0], tex_v=CELL if not _is_crate(info) else info[1] * 2)
        glEnd()

    # ---- building details: a window on each outer face, a ledge round the roof
    glDisable(GL_TEXTURE_2D)
    glBegin(GL_QUADS)
    for r in range(ROWS):
        for c in range(COLS):
            info = cell_info(r, c)
            if info[0] != "flat" or info[1] < 2.0:
                continue
            h = info[1]
            for (dr, dc), n, p0, p1 in faces_of(r, c):
                below = _edge_top(r + dr, c + dc, (-dr, -dc))
                if below > 0.3 or solid_cell(r + dr, c + dc):
                    continue
                ox, oz = n[0] * 0.012, n[2] * 0.012
                mx, mz = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
                tx, tz = (p1[0] - p0[0]) / CELL, (p1[1] - p0[1]) / CELL
                lit = not in_sun_shadow(mx + n[0] * 0.1, h * 0.55, mz + n[2] * 0.1)
                for half_w, y0, y1, col, off in ((0.46, h * 0.30, h * 0.80, (0.30, 0.29, 0.27), 1.0),
                                                 (0.38, h * 0.34, h * 0.76, (0.10, 0.14, 0.20), 2.0),
                                                 (0.03, h * 0.34, h * 0.76, (0.30, 0.29, 0.27), 3.0),
                                                 (0.38, h * 0.54, h * 0.56, (0.30, 0.29, 0.27), 3.0)):
                    k = light(n[0], n[1], n[2], lit, 0.9)
                    glColor3f(*(col[i] * k[i] * 1.4 for i in range(3)))
                    for u, y in ((-half_w, y0), (half_w, y0), (half_w, y1), (-half_w, y1)):
                        glVertex3f(mx + tx * u + ox * off, y, mz + tz * u + oz * off)
                # roof ledge: a strip sticking out 6 cm, 12 cm high
                k = light(n[0], n[1], n[2], lit, 1.0)
                glColor3f(*(0.62 * k[i] for i in range(3)))
                ex, ez = n[0] * 0.06, n[2] * 0.06
                for (ax, az), y in (((p0[0] + ex, p0[1] + ez), h - 0.12), ((p1[0] + ex, p1[1] + ez), h - 0.12),
                                    ((p1[0] + ex, p1[1] + ez), h + 0.02), ((p0[0] + ex, p0[1] + ez), h + 0.02)):
                    glVertex3f(ax, y, az)
                kt = light(0, 1, 0, True, 1.0)
                glColor3f(*(0.66 * kt[i] for i in range(3)))
                for (ax, az) in ((p0[0], p0[1]), (p0[0] + ex, p0[1] + ez), (p1[0] + ex, p1[1] + ez), (p1[0], p1[1])):
                    glVertex3f(ax, h + 0.02, az)
    glEnd()
    glEnable(GL_TEXTURE_2D)

    # ---- stairs: treads, risers and side walls of each step
    glBindTexture(GL_TEXTURE_2D, tex_floor)
    glBegin(GL_QUADS)
    for r in range(ROWS):
        for c in range(COLS):
            info = cell_info(r, c)
            if info[0] != "ramp":
                continue
            _, axis, h0, h1 = info
            x0, z0 = c * CELL, r * CELL
            for k in range(STAIR_STEPS):
                a, b = k / STAIR_STEPS, (k + 1) / STAIR_STEPS
                # tread height: the slope at the middle of the step; numbered from the low-coordinate edge
                y = h0 + (h1 - h0) * (a + b) / 2
                if axis == "x":
                    quad_h(x0 + a * CELL, z0, x0 + b * CELL, z0 + CELL, y, 1)
                else:
                    quad_h(x0, z0 + a * CELL, x0 + CELL, z0 + b * CELL, y, 1)
                # riser on the downhill side of this tread, up from the next lower tread (or the floor)
                lo = max(0.0, y - abs(h1 - h0) / STAIR_STEPS)
                if h1 > h0:                                   # uphill towards +axis: riser at a
                    if axis == "x":
                        quad_v((x0 + a * CELL, z0), (x0 + a * CELL, z0 + CELL), lo, y, (-1, 0, 0))
                    else:
                        quad_v((x0 + CELL, z0 + a * CELL), (x0, z0 + a * CELL), lo, y, (0, 0, -1))
                else:                                         # uphill towards -axis: riser at b
                    if axis == "x":
                        quad_v((x0 + b * CELL, z0 + CELL), (x0 + b * CELL, z0), lo, y, (1, 0, 0))
                    else:
                        quad_v((x0, z0 + b * CELL), (x0 + CELL, z0 + b * CELL), lo, y, (0, 0, 1))
                # side walls of this step, where the neighbour is lower (not another stair of the same run)
                for (dr, dc), n, p0, p1 in faces_of(r, c):
                    if (axis == "x" and dc != 0) or (axis == "z" and dr != 0):
                        continue                              # front / back: risers and the next cell
                    nb = cell_info(r + dr, c + dc)
                    if nb[0] == "ramp" and nb[1] == axis:
                        continue
                    below = _edge_top(r + dr, c + dc, (-dr, -dc))
                    if below >= y:
                        continue
                    if axis == "x":
                        q0 = (x0 + a * CELL, p0[1]) if n[2] > 0 else (x0 + b * CELL, p0[1])
                        q1 = (x0 + b * CELL, p0[1]) if n[2] > 0 else (x0 + a * CELL, p0[1])
                    else:
                        q0 = (p0[0], z0 + b * CELL) if n[0] > 0 else (p0[0], z0 + a * CELL)
                        q1 = (p0[0], z0 + a * CELL) if n[0] > 0 else (p0[0], z0 + b * CELL)
                    quad_v(q0, q1, max(0.0, below), y, n)
    glEnd()

    # ---- walls: every face next to a non-wall cell, from that cell's top up
    glBindTexture(GL_TEXTURE_2D, tex_wall)
    HS = [0.25, 0.6, 1.2, 1.8, 2.4, 3.0, 3.5]
    glBegin(GL_QUADS)
    for r in range(ROWS):
        for c in range(COLS):
            if not solid_cell(r, c):
                continue
            x0, z0, x1, z1 = c * CELL, r * CELL, (c + 1) * CELL, (r + 1) * CELL
            for (dr, dc), n, p0, p1 in faces_of(r, c):
                if solid_cell(r + dr, c + dc):
                    continue
                quad_v(p0, p1, max(0.0, min(_edge_top(r + dr, c + dc, (-dr, -dc)), WALL_H)), WALL_H, n, HS)
            glColor3f(*(v * 0.75 for v in light(0, 1, 0, True)))      # wall top
            for vx, vz in ((x0, z0), (x0, z1), (x1, z1), (x1, z0)):
                glTexCoord2f(vx / CELL * 2, vz / CELL * 2)
                glVertex3f(vx, WALL_H, vz)
    glEnd()
    glDisable(GL_TEXTURE_2D)
    glEndList()
    return lst


# ------------------------------------------------------------------ primitive helpers (lit)
def box(x0, y0, z0, x1, y1, z1, col):
    """Axis-aligned box with normals (for GL lighting + colour material)."""
    glColor3f(*col)
    glBegin(GL_QUADS)
    glNormal3f(0, 1, 0)
    glVertex3f(x0, y1, z0); glVertex3f(x0, y1, z1); glVertex3f(x1, y1, z1); glVertex3f(x1, y1, z0)
    glNormal3f(0, -1, 0)
    glVertex3f(x0, y0, z0); glVertex3f(x1, y0, z0); glVertex3f(x1, y0, z1); glVertex3f(x0, y0, z1)
    glNormal3f(0, 0, 1)
    glVertex3f(x0, y0, z1); glVertex3f(x1, y0, z1); glVertex3f(x1, y1, z1); glVertex3f(x0, y1, z1)
    glNormal3f(0, 0, -1)
    glVertex3f(x0, y0, z0); glVertex3f(x0, y1, z0); glVertex3f(x1, y1, z0); glVertex3f(x1, y0, z0)
    glNormal3f(-1, 0, 0)
    glVertex3f(x0, y0, z0); glVertex3f(x0, y0, z1); glVertex3f(x0, y1, z1); glVertex3f(x0, y1, z0)
    glNormal3f(1, 0, 0)
    glVertex3f(x1, y0, z0); glVertex3f(x1, y1, z0); glVertex3f(x1, y1, z1); glVertex3f(x1, y0, z1)
    glEnd()


def beam(p0, p1, w, h, col):
    """Box of width w / height h running from point p0 to p1 (for arms)."""
    d = [p1[i] - p0[i] for i in range(3)]
    L = math.sqrt(sum(v * v for v in d)) or 1e-6
    d = [v / L for v in d]
    up = (0.0, 1.0, 0.0) if abs(d[1]) < 0.9 else (1.0, 0.0, 0.0)
    sx = [d[1] * up[2] - d[2] * up[1], d[2] * up[0] - d[0] * up[2], d[0] * up[1] - d[1] * up[0]]
    sl = math.sqrt(sum(v * v for v in sx))
    sx = [v / sl for v in sx]
    uy = [sx[1] * d[2] - sx[2] * d[1], sx[2] * d[0] - sx[0] * d[2], sx[0] * d[1] - sx[1] * d[0]]

    def v(end, a, b):
        base = p1 if end else p0
        return [base[i] + sx[i] * a * w / 2 + uy[i] * b * h / 2 for i in range(3)]

    glColor3f(*col)
    glBegin(GL_QUADS)
    for (a0, b0), (a1, b1), n in (((-1, 1), (1, 1), uy), ((1, -1), (-1, -1), [-c for c in uy]),
                                  ((1, 1), (1, -1), sx), ((-1, -1), (-1, 1), [-c for c in sx])):
        glNormal3f(*n)
        glVertex3f(*v(0, a0, b0)); glVertex3f(*v(1, a0, b0)); glVertex3f(*v(1, a1, b1)); glVertex3f(*v(0, a1, b1))
    for end, sign in ((0, -1), (1, 1)):
        glNormal3f(*(c * sign for c in d))
        for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            glVertex3f(*v(end, a, b))
    glEnd()


def cbox(cx, cy, cz, sx, sy, sz, col):
    """Box by centre and size."""
    box(cx - sx / 2, cy - sy / 2, cz - sz / 2, cx + sx / 2, cy + sy / 2, cz + sz / 2, col)


def mul(c, k):
    return tuple(min(1.0, v * k) for v in c)


# ------------------------------------------------------------------ HUD text
class Text:
    def __init__(self):
        pygame.font.init()
        self.fonts = {}
        self.cache = {}

    def font(self, size):
        if size not in self.fonts:
            self.fonts[size] = pygame.font.SysFont("dejavusans,verdana,arial,sans", size, bold=True)
        return self.fonts[size]

    def draw(self, x, y, s, color=(255, 255, 255), size=20, align="left"):
        """x, y = bottom-left in window pixels (origin bottom-left)."""
        key = (s, color, size)
        if key not in self.cache:
            if len(self.cache) > 400:
                self.cache.clear()
            f = self.font(size)
            surf = f.render(s, True, color)
            shadow = f.render(s, True, (0, 0, 0))
            w, h = surf.get_size()
            comp = pygame.Surface((w + 2, h + 2), pygame.SRCALPHA)
            comp.blit(shadow, (2, 2))
            comp.blit(surf, (0, 0))
            self.cache[key] = (comp.get_width(), comp.get_height(),
                               pygame.image.tostring(comp, "RGBA", True))
        w, h, data = self.cache[key]
        if align == "center":
            x -= w / 2
        elif align == "right":
            x -= w
        glWindowPos2f(max(0.0, x), max(0.0, y))
        glDrawPixels(w, h, GL_RGBA, GL_UNSIGNED_BYTE, data)
        return w


def rect(x0, y0, x1, y1, color, a=1.0):
    glColor4f(color[0], color[1], color[2], a)
    glBegin(GL_QUADS)
    glVertex2f(x0, y0); glVertex2f(x1, y0); glVertex2f(x1, y1); glVertex2f(x0, y1)
    glEnd()


# ------------------------------------------------------------------ renderer
class RendererBase:
    def __init__(self, W, H, fov=75.0, seed=7):
        self.W, self.H, self.fov = W, H, fov
        rng = random.Random(seed)
        glViewport(0, 0, W, H)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glEnable(GL_CULL_FACE)
        glCullFace(GL_BACK)
        glFrontFace(GL_CCW)
        try:
            glEnable(GL_MULTISAMPLE)
        except Exception:
            pass
        glShadeModel(GL_SMOOTH)
        glTexEnvi(GL_TEXTURE_ENV, GL_TEXTURE_ENV_MODE, GL_MODULATE)
        glHint(GL_PERSPECTIVE_CORRECTION_HINT, GL_NICEST)
        # fog
        glFogi(GL_FOG_MODE, GL_LINEAR)
        glFogfv(GL_FOG_COLOR, (*SKY_HORIZON, 1.0))
        glFogf(GL_FOG_START, FOG_START)
        glFogf(GL_FOG_END, FOG_END)
        # lighting for dynamic objects (players, gun)
        glLightModelfv(GL_LIGHT_MODEL_AMBIENT, (0.0, 0.0, 0.0, 1.0))
        glLightfv(GL_LIGHT0, GL_AMBIENT, (*AMB_COL, 1.0))
        glLightfv(GL_LIGHT0, GL_SPECULAR, (0.0, 0.0, 0.0, 1.0))
        glEnable(GL_LIGHT0)
        glColorMaterial(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE)
        glEnable(GL_NORMALIZE)
        glClearColor(*SKY_HORIZON, 1.0)

        self.tex_wall = upload_texture(make_brick_texture(rng), TEX, GL_RGB)
        self.tex_floor = upload_texture(make_floor_texture(rng), TEX, GL_RGB)
        self.tex_crate = upload_texture(make_crate_texture(rng), TEX, GL_RGB)
        cdata, csize = make_cloud_texture(rng)
        self.tex_cloud = upload_texture(cdata, csize, GL_RGBA)
        self.world = build_world_list(self.tex_wall, self.tex_floor, self.tex_crate)

        self.txt = Text()
        self.particles = []          # [x, y, z, vx, vy, vz, life, max_life, r, g, b, size]
        self.decals = []             # [x, y, z, nx, nz, ttl]  (wall bullet holes)
        self.walk_phase = 0.0
        self.bob_phase = 0.0
        self.bob_amp = 0.0
        self.sway = [0.0, 0.0]
        self.last_me = None
        self.last_rv = None
        self.rv_speed = 0.0
        self.prev_tracer = 0.0
        self.clock = 0.0
        self.rng = rng

    # ---------------------------------------------------------- projection helpers
    def perspective(self, fov, near, far):
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        top = near * math.tan(math.radians(fov) / 2)
        right = top * self.W / self.H
        glFrustum(-right, right, -top, top, near, far)
        glMatrixMode(GL_MODELVIEW)

    def ortho(self):
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0, self.W, 0, self.H, -1, 1)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()

    # ---------------------------------------------------------- sky
    def draw_sky(self):
        glDisable(GL_FOG)
        glDisable(GL_DEPTH_TEST)
        glDepthMask(GL_FALSE)
        glDisable(GL_CULL_FACE)
        R = 50.0
        rings = [(-20, SKY_HORIZON), (0, SKY_HORIZON), (8, None), (25, None), (50, None), (90, SKY_ZENITH)]

        def col(el):
            t = max(0.0, min(1.0, el / 90.0)) ** 0.6
            return tuple(SKY_HORIZON[i] + (SKY_ZENITH[i] - SKY_HORIZON[i]) * t for i in range(3))

        segs = 24
        for k in range(len(rings) - 1):
            e0, e1 = rings[k][0], rings[k + 1][0]
            glBegin(GL_QUAD_STRIP)
            for sgi in range(segs + 1):
                a = 2 * math.pi * sgi / segs
                for e in (e0, e1):
                    er = math.radians(e)
                    glColor3f(*col(e))
                    glVertex3f(R * math.cos(er) * math.cos(a), R * math.sin(er), R * math.cos(er) * math.sin(a))
            glEnd()
        # sun: soft glow + disc (additive)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        sx, sy, sz = (c * R * 0.9 for c in SUN_DIR)
        ux, uz = -SUN_DIR[2], SUN_DIR[0]                 # horizontal axis perpendicular to sun
        ul = math.hypot(ux, uz)
        ux, uz = ux / ul, uz / ul
        vx = SUN_DIR[1] * uz                             # v = sun x u
        vy = SUN_DIR[2] * ux - SUN_DIR[0] * uz
        vz = -SUN_DIR[1] * ux
        for size, a in ((9.0, 0.18), (4.0, 0.35), (1.6, 1.0)):
            glBegin(GL_TRIANGLE_FAN)
            glColor4f(1.0, 0.95, 0.80, a)
            glVertex3f(sx, sy, sz)
            glColor4f(1.0, 0.90, 0.70, 0.0 if size > 2 else a)
            for i in range(25):
                t = 2 * math.pi * i / 24
                cx, cy = math.cos(t) * size, math.sin(t) * size
                glVertex3f(sx + ux * cx + vx * cy, sy + vy * cy, sz + uz * cx + vz * cy)
            glEnd()
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDepthMask(GL_TRUE)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)

    def draw_clouds(self, camx, camz):
        glDisable(GL_FOG)
        glDepthMask(GL_FALSE)
        glDisable(GL_CULL_FACE)
        glEnable(GL_TEXTURE_2D)
        glBindTexture(GL_TEXTURE_2D, self.tex_cloud)
        Hc, span, n = 45.0, 260.0, 10
        drift = self.clock * 0.004
        glBegin(GL_QUADS)
        for i in range(n):
            for j in range(n):
                for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1)):
                    u, v = (i + di) / n - 0.5, (j + dj) / n - 0.5
                    x, z = camx + u * span, camz + v * span
                    fade = max(0.0, 1.0 - math.hypot(u, v) * 2.0)
                    glColor4f(1, 1, 1, 0.85 * fade)
                    glTexCoord2f(x / 90.0 + drift, z / 90.0)
                    glVertex3f(x, Hc, z)
        glEnd()
        glDisable(GL_TEXTURE_2D)
        glEnable(GL_CULL_FACE)
        glDepthMask(GL_TRUE)
        glEnable(GL_FOG)

    # ---------------------------------------------------------- dynamic lighting
    def set_sun(self, shadowed):
        glLightfv(GL_LIGHT0, GL_POSITION, (*SUN_DIR, 0.0))
        k = 0.12 if shadowed else 0.85
        glLightfv(GL_LIGHT0, GL_DIFFUSE, (SUN_COL[0] * k, SUN_COL[1] * k, SUN_COL[2] * k, 1.0))

    # ---------------------------------------------------------- players
    def draw_blob_shadow(self, x, z, r=0.45, y=None):
        g = floor_at(x, z) if y is None else y
        if g == INF:
            return
        g += 0.015
        glDisable(GL_CULL_FACE)
        glDepthMask(GL_FALSE)
        glBegin(GL_TRIANGLE_FAN)
        glColor4f(0, 0, 0, 0.45)
        glVertex3f(x, g, z)
        glColor4f(0, 0, 0, 0.0)
        for i in range(21):
            a = 2 * math.pi * i / 20
            glVertex3f(x + math.cos(a) * r, g, z + math.sin(a) * r)
        glEnd()
        glDepthMask(GL_TRUE)
        glEnable(GL_CULL_FACE)

    def draw_player(self, rv, pid, dead, flash, firing, dt):
        team = TEAM.get(pid, (0.8, 0.8, 0.2))
        if flash > 0:
            team = (1.0, 0.85, 0.85)
        dark = (0.13, 0.14, 0.16)
        cloth = (0.24, 0.26, 0.22)
        skin = (0.86, 0.68, 0.52)
        self.draw_blob_shadow(rv.x, rv.z)
        glEnable(GL_LIGHTING)
        glEnable(GL_COLOR_MATERIAL)
        self.set_sun(in_sun_shadow(rv.x, rv.y + 1.2, rv.z))
        glPushMatrix()
        glTranslatef(rv.x, rv.y, rv.z)
        glRotatef(-math.degrees(rv.yaw), 0, 1, 0)
        if dead:
            glTranslatef(0, 0.18, 0)
            glRotatef(-90, 1, 0, 0)
            glTranslatef(0, 0, 0.0)
            team = mul(team, 0.45)
        swing = 0.0 if dead or rv.y > 0.05 else math.sin(self.walk_phase) * min(1.0, self.rv_speed / 4.0) * 35
        # legs (pivot at hip)
        for side, sgn in ((-0.11, 1), (0.11, -1)):
            glPushMatrix()
            glTranslatef(side, 0.86, 0)
            glRotatef(swing * sgn, 1, 0, 0)
            cbox(0, -0.40, 0, 0.17, 0.80, 0.20, cloth)
            cbox(0, -0.80, -0.04, 0.19, 0.14, 0.30, dark)              # boot
            glPopMatrix()
        # torso, vest, belt
        cbox(0, 1.16, 0, 0.50, 0.62, 0.28, mul(team, 0.85))
        cbox(0, 1.20, -0.01, 0.44, 0.44, 0.32, team)                    # vest plate
        cbox(0, 0.88, 0, 0.48, 0.07, 0.30, dark)                        # belt
        cbox(0, 1.30, -0.165, 0.30, 0.10, 0.03, mul(team, 0.6))        # chest pouch
        # head + helmet + visor
        cbox(0, 1.60, 0, 0.24, 0.26, 0.24, skin)
        cbox(0, 1.73, 0.01, 0.30, 0.12, 0.30, mul(team, 0.7))           # helmet top
        cbox(0, 1.665, 0.12, 0.30, 0.10, 0.06, mul(team, 0.7))          # helmet back
        cbox(0, 1.63, -0.125, 0.22, 0.06, 0.02, (0.05, 0.06, 0.08))     # visor
        # arms + gun follow pitch (pivot at shoulders)
        glPushMatrix()
        glTranslatef(0, 1.38, 0)
        glRotatef(math.degrees(rv.pitch), 1, 0, 0)
        cbox(-0.30, -0.08, -0.12, 0.13, 0.13, 0.36, mul(team, 0.85))     # left arm
        cbox(0.30, -0.08, -0.06, 0.13, 0.13, 0.30, mul(team, 0.85))      # right arm
        cbox(-0.16, -0.10, -0.38, 0.10, 0.10, 0.12, skin)                # hands
        cbox(0.18, -0.12, -0.20, 0.10, 0.10, 0.10, skin)
        cbox(0.06, -0.06, -0.40, 0.08, 0.10, 0.70, dark)                 # rifle body
        cbox(0.06, -0.05, -0.85, 0.035, 0.035, 0.22, (0.08, 0.08, 0.08)) # barrel
        cbox(0.06, -0.15, -0.30, 0.05, 0.16, 0.08, (0.10, 0.10, 0.10))   # mag
        glPopMatrix()
        glPopMatrix()
        glDisable(GL_LIGHTING)
        glDisable(GL_COLOR_MATERIAL)

        if firing and not dead:                                          # opponent muzzle flash
            d = look_dir(rv.yaw, rv.pitch)
            rx, rz = math.cos(rv.yaw), math.sin(rv.yaw)
            mx = rv.x + rx * 0.06 + d[0] * 0.95
            my = rv.y + 1.32 + d[1] * 0.95
            mz = rv.z + rz * 0.06 + d[2] * 0.95
            self.flash_sprite(mx, my, mz, 0.35)

        if not dead:                                                     # HP bar over head
            self.billboard_bar(rv.x, rv.y + 2.08, rv.z, rv.state.hp / MAX_HP if rv.state else 1.0, team)

    def billboard_bar(self, x, y, z, frac, team):
        ry = self.cam_yaw
        rx, rz = math.cos(ry), math.sin(ry)
        w, h = 0.40, 0.055
        glDisable(GL_CULL_FACE)
        glDisable(GL_FOG)

        def q(x0, x1, y0, y1, c, a):
            glColor4f(c[0], c[1], c[2], a)
            glBegin(GL_QUADS)
            glVertex3f(x + rx * x0, y0, z + rz * x0); glVertex3f(x + rx * x1, y0, z + rz * x1)
            glVertex3f(x + rx * x1, y1, z + rz * x1); glVertex3f(x + rx * x0, y1, z + rz * x0)
            glEnd()

        q(-w - 0.015, w + 0.015, y - h - 0.015, y + h + 0.015, (0, 0, 0), 0.55)
        hc = (0.25 + 0.7 * (1 - frac), 0.85 * frac + 0.1, 0.2)
        q(-w, -w + 2 * w * frac, y - h, y + h, hc, 0.95)
        glEnable(GL_FOG)
        glEnable(GL_CULL_FACE)

    def flash_sprite(self, x, y, z, size, alpha=1.0):
        """Camera-facing additive star for muzzle flashes."""
        ry, rp = self.cam_yaw, self.cam_pitch
        rx, rz = math.cos(ry), math.sin(ry)
        ux, uy, uz = -math.sin(ry) * math.sin(rp), math.cos(rp), math.cos(ry) * math.sin(rp)
        glDisable(GL_CULL_FACE)
        glDepthMask(GL_FALSE)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        rot = self.rng.random() * math.pi
        glBegin(GL_TRIANGLES)
        for k in range(5):
            a = rot + 2 * math.pi * k / 5
            c0, s0 = math.cos(a - 0.35) * 0.3, math.sin(a - 0.35) * 0.3
            c1, s1 = math.cos(a), math.sin(a)
            c2, s2 = math.cos(a + 0.35) * 0.3, math.sin(a + 0.35) * 0.3
            glColor4f(1.0, 0.95, 0.7, alpha)
            glVertex3f(x, y, z)
            glColor4f(1.0, 0.6, 0.15, 0.0)
            for cc, ss in ((c0, s0), (c1, s1)):
                glVertex3f(x + (rx * cc + ux * ss) * size, y + uy * ss * size, z + (rz * cc + uz * ss) * size)
            glColor4f(1.0, 0.95, 0.7, alpha)
            glVertex3f(x, y, z)
            glColor4f(1.0, 0.6, 0.15, 0.0)
            for cc, ss in ((c1, s1), (c2, s2)):
                glVertex3f(x + (rx * cc + ux * ss) * size, y + uy * ss * size, z + (rz * cc + uz * ss) * size)
        glEnd()
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDepthMask(GL_TRUE)
        glEnable(GL_CULL_FACE)

    def plus_sprite(self, x, y, z, size, col=(0.25, 1.0, 0.35), alpha=0.9):
        """Camera-facing green plus (healing)."""
        ry = self.cam_yaw
        rx, rz = math.cos(ry), math.sin(ry)
        glDisable(GL_CULL_FACE)
        glDisable(GL_FOG)
        glColor4f(*col, alpha)
        glBegin(GL_QUADS)
        for (a0, a1, b0, b1) in ((-1, 1, -0.33, 0.33), (-0.33, 0.33, -1, 1)):
            for a, b in ((a0, b0), (a1, b0), (a1, b1), (a0, b1)):
                glVertex3f(x + rx * a * size, y + b * size, z + rz * a * size)
        glEnd()
        glEnable(GL_FOG)
        glEnable(GL_CULL_FACE)

    def draw_pickups(self, pickups):
        """Medkits: white boxes with red crosses, turning and bobbing, with a soft green ring on the ground."""
        if not pickups:
            return
        for x, y, z, kind in pickups:
            self.draw_blob_shadow(x, z, 0.5, y)
            glDisable(GL_CULL_FACE)
            glDepthMask(GL_FALSE)
            glBegin(GL_TRIANGLE_STRIP)
            pulse = 0.25 + 0.15 * math.sin(self.clock * 4 + x)
            for i in range(25):
                a = 2 * math.pi * i / 24
                for rr, al in ((0.55, pulse), (0.75, 0.0)):
                    glColor4f(0.3, 1.0, 0.4, al)
                    glVertex3f(x + math.cos(a) * rr, y + 0.03, z + math.sin(a) * rr)
            glEnd()
            glDepthMask(GL_TRUE)
            glEnable(GL_CULL_FACE)
        glEnable(GL_LIGHTING)
        glEnable(GL_COLOR_MATERIAL)
        self.set_sun(False)
        for x, y, z, kind in pickups:
            s = 1.0 if kind == "spot" else 0.8
            glPushMatrix()
            glTranslatef(x, y + 0.45 + 0.08 * math.sin(self.clock * 2.5 + z), z)
            glRotatef(math.degrees(self.clock * 1.4 + x), 0, 1, 0)
            glScalef(s, s, s)
            cbox(0, 0, 0, 0.46, 0.30, 0.32, (0.93, 0.93, 0.91))
            cbox(0, 0.16, 0, 0.22, 0.04, 0.06, (0.35, 0.35, 0.36))                  # handle
            for sgn in (-1, 1):
                cbox(0, 0, sgn * 0.162, 0.26, 0.08, 0.004, (0.85, 0.08, 0.08))
                cbox(0, 0, sgn * 0.162, 0.08, 0.22, 0.004, (0.85, 0.08, 0.08))
                cbox(sgn * 0.232, 0, 0, 0.004, 0.08, 0.18, (0.85, 0.08, 0.08))
                cbox(sgn * 0.232, 0, 0, 0.004, 0.22, 0.06, (0.85, 0.08, 0.08))
            glPopMatrix()
        glDisable(GL_LIGHTING)
        glDisable(GL_COLOR_MATERIAL)

    # ---------------------------------------------------------- effects
    def spawn_impact(self, end, d, kind):
        ex, ey, ez = end
        if kind in ("head", "body"):
            col, n_p, spd = (0.75, 0.05, 0.05), 10, 2.0
            normal = (-d[0], -d[1], -d[2])
        elif kind == "floor":
            col, n_p, spd = (1.0, 0.85, 0.5), 8, 2.5
            normal = (0.0, 1.0, 0.0)
        elif kind == "wall":
            col, n_p, spd = (1.0, 0.85, 0.5), 8, 2.5
            q = (ex - d[0] * 0.06, ez - d[2] * 0.06)
            s = (ex + d[0] * 0.06, ez + d[2] * 0.06)
            if int(q[0] // CELL) != int(s[0] // CELL):
                normal = (-math.copysign(1, d[0]), 0.0, 0.0)
                cx = int(s[0] // CELL)
                ex = cx * CELL if d[0] > 0 else (cx + 1) * CELL
            else:
                normal = (0.0, 0.0, -math.copysign(1, d[2]))
                cz = int(s[1] // CELL)
                ez = cz * CELL if d[2] > 0 else (cz + 1) * CELL
            self.decals.append([ex + normal[0] * 0.012, ey, ez + normal[2] * 0.012, normal[0], normal[2], 10.0])
            if len(self.decals) > 60:
                self.decals.pop(0)
            col = (1.0, 0.85, 0.5)
            # dust puff
            for _ in range(5):
                self.particles.append([ex, ey, ez,
                                       normal[0] * 0.6 + self.rng.uniform(-0.3, 0.3),
                                       self.rng.uniform(0.0, 0.5),
                                       normal[2] * 0.6 + self.rng.uniform(-0.3, 0.3),
                                       0.5, 0.5, 0.72, 0.68, 0.60, 7.0])
        else:
            return
        for _ in range(n_p):
            vx = normal[0] * spd + self.rng.uniform(-1.2, 1.2)
            vy = normal[1] * spd + self.rng.uniform(0.2, 1.8)
            vz = normal[2] * spd + self.rng.uniform(-1.2, 1.2)
            life = self.rng.uniform(0.15, 0.35)
            self.particles.append([ex, ey, ez, vx, vy, vz, life, life, *col, 3.0])

    def update_effects(self, dt):
        for p in self.particles:
            p[0] += p[3] * dt
            p[1] += p[4] * dt
            p[2] += p[5] * dt
            p[4] -= 9.0 * dt if p[11] < 5 else -0.4 * dt
            p[6] -= dt
        self.particles = [p for p in self.particles if p[6] > 0]
        for dcl in self.decals:
            dcl[5] -= dt
        self.decals = [dcl for dcl in self.decals if dcl[5] > 0]

    def draw_effects(self):
        # bullet holes: small dark discs with a soft rim
        glDisable(GL_CULL_FACE)
        glDepthMask(GL_FALSE)
        glEnable(GL_POLYGON_OFFSET_FILL)
        glPolygonOffset(-1.0, -2.0)
        for x, y, z, nx, nz, ttl in self.decals:
            a = min(1.0, ttl / 2.0)
            tx, tz = -nz, nx                       # tangent along the wall
            glBegin(GL_TRIANGLE_FAN)
            glColor4f(0.05, 0.045, 0.04, 0.9 * a)
            glVertex3f(x, y, z)
            for i in range(9):
                t = 2 * math.pi * i / 8
                c, sn = math.cos(t) * 0.018, math.sin(t) * 0.018
                glColor4f(0.18, 0.16, 0.14, 0.0)
                glVertex3f(x + tx * c, y + sn, z + tz * c)
            glEnd()
            glBegin(GL_TRIANGLE_FAN)
            glColor4f(0.02, 0.02, 0.02, 0.95 * a)
            glVertex3f(x, y, z)
            for i in range(9):
                t = 2 * math.pi * i / 8
                c, sn = math.cos(t) * 0.007, math.sin(t) * 0.007
                glVertex3f(x + tx * c, y + sn, z + tz * c)
            glEnd()
        glDisable(GL_POLYGON_OFFSET_FILL)
        # sparks (additive points) and dust (alpha points)
        glEnable(GL_POINT_SMOOTH)
        for additive in (True, False):
            glBlendFunc(GL_SRC_ALPHA, GL_ONE if additive else GL_ONE_MINUS_SRC_ALPHA)
            for size in sorted({p[11] for p in self.particles if (p[11] < 5) == additive}):
                glPointSize(size)
                glBegin(GL_POINTS)
                for p in self.particles:
                    if p[11] == size:
                        a = p[6] / p[7]
                        glColor4f(p[8], p[9], p[10], a * (1.0 if additive else 0.5))
                        glVertex3f(p[0], p[1], p[2])
                glEnd()
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDepthMask(GL_TRUE)
        glEnable(GL_CULL_FACE)

    def muzzle_world(self, me):
        """Approximate world position of the first-person gun muzzle."""
        d = look_dir(me.yaw, me.pitch)
        rx, rz = math.cos(me.yaw), math.sin(me.yaw)
        ux, uy, uz = -math.sin(me.yaw) * math.sin(me.pitch), math.cos(me.pitch), math.cos(me.yaw) * math.sin(me.pitch)
        ex, ey, ez = me.eye()
        f, r, u = (1.15 if me.weapon == W_SNIPER else 0.85), 0.13, -0.13
        return (ex + d[0] * f + rx * r + ux * u, ey + d[1] * f + uy * u, ez + d[2] * f + rz * r + uz * u)

    def draw_tracers(self, m):
        glDisable(GL_FOG)
        glDepthMask(GL_FALSE)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        glLineWidth(2.5)
        glBegin(GL_LINES)
        mz = self.muzzle_world(m.me)
        for (_s, e, ttl) in m.tracers:
            s = mz
            a = ttl / 0.06
            # tracer segment: a short bright streak travelling along the shot
            t0, t1 = (1 - a) * 0.6, (1 - a) * 0.6 + 0.4
            glColor4f(1.0, 0.85, 0.45, 0.0)
            glVertex3f(*(s[i] + (e[i] - s[i]) * t0 for i in range(3)))
            glColor4f(1.0, 0.95, 0.7, 0.9)
            glVertex3f(*(s[i] + (e[i] - s[i]) * t1 for i in range(3)))
        rv = m.remote
        if rv.connected(m.now) and rv.tracer_t > 0 and rv.alive:
            d = look_dir(rv.yaw, rv.pitch)
            rx, rz = math.cos(rv.yaw), math.sin(rv.yaw)
            ex, ey, ez = rv.x + rx * 0.06 + d[0] * 0.95, rv.y + 1.32 + d[1] * 0.95, rv.z + rz * 0.06 + d[2] * 0.95
            dist = self.remote_shot_dist
            glColor4f(1.0, 0.6, 0.3, 0.0)
            glVertex3f(ex, ey, ez)
            glColor4f(1.0, 0.75, 0.4, 0.85)
            glVertex3f(ex + d[0] * dist, ey + d[1] * dist, ez + d[2] * dist)
        glEnd()
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDepthMask(GL_TRUE)
        glEnable(GL_FOG)

    # ---------------------------------------------------------- first-person gun
    def draw_viewmodel(self, me, recoil, team):
        glClear(GL_DEPTH_BUFFER_BIT)
        self.perspective(60.0, 0.02, 10.0)
        glLoadIdentity()
        glDisable(GL_FOG)
        glEnable(GL_LIGHTING)
        glEnable(GL_COLOR_MATERIAL)
        # light in view space: rotate the sun into camera coordinates
        sx, sy, sz = SUN_DIR
        cy, sy_ = math.cos(self.cam_yaw), math.sin(self.cam_yaw)
        vx, vz = sx * cy + sz * sy_, -sx * sy_ + sz * cy
        cp, sp = math.cos(-self.cam_pitch), math.sin(-self.cam_pitch)
        vy, vz2 = sy * cp - vz * sp, sy * sp + vz * cp
        glLightfv(GL_LIGHT0, GL_POSITION, (vx, vy, vz2, 0.0))
        k = 0.22 if in_sun_shadow(me.x, me.y + 1.4, me.z) else 0.85
        glLightfv(GL_LIGHT0, GL_DIFFUSE, (SUN_COL[0] * k, SUN_COL[1] * k, SUN_COL[2] * k, 1.0))

        w = me.weapon
        wd = WEAPONS[w]
        rl, p = 0.0, 0.0
        if me.reloading and wd["reload"] > 0:
            p = 1.0 - me.reload_t / wd["reload"]
            rl = math.sin(math.pi * min(1.0, max(0.0, p)))
        dip = 0.0
        if me.switch_t > 0:                                       # new weapon comes up from below
            dip = me.switch_t / SWITCH_TIME
        if me.healing:
            dip = max(dip, 0.55)
        bx = math.sin(self.bob_phase) * 0.012 * self.bob_amp
        by = -abs(math.cos(self.bob_phase)) * 0.010 * self.bob_amp
        metal = (0.30, 0.31, 0.33)
        metal_d = (0.17, 0.17, 0.19)
        poly = (0.33, 0.30, 0.24)
        glove = (0.15, 0.14, 0.12)
        sleeve = mul(team, 0.80)

        if me.healing:                                            # left hand holds up the medkit
            glPushMatrix()
            k = min(1.0, (HEAL_TIME - me.heal_t) * 4.0)
            glTranslatef(-0.17 + bx, -0.21 + 0.08 * k + by, -0.46)
            glRotatef(25, 0, 1, 0)
            glRotatef(-10, 1, 0, 0)
            glScalef(0.6, 0.6, 0.6)
            box(-0.07, -0.05, -0.05, 0.07, 0.05, 0.05, (0.93, 0.93, 0.91))
            for zf in (0.0505, -0.0525):                          # red cross front and back
                box(-0.04, -0.012, zf, 0.04, 0.012, zf + 0.002, (0.85, 0.08, 0.08))
                box(-0.012, -0.035, zf, 0.012, 0.035, zf + 0.002, (0.85, 0.08, 0.08))
            box(-0.025, 0.05, -0.012, 0.025, 0.062, 0.012, (0.35, 0.35, 0.36))
            box(-0.04, -0.09, 0.0, 0.04, -0.05, 0.05, glove)
            beam((0.0, -0.08, 0.04), (-0.20, -0.45, 0.30), 0.06, 0.065, sleeve)
            glPopMatrix()

        if w == W_KNIFE:
            swing = 0.0
            if me.fire_cd > 0:                                    # slash: across from right to left
                q = 1.0 - me.fire_cd / wd["interval"]
                swing = math.sin(math.pi * min(1.0, q * 1.6))
            glTranslatef(0.17 + bx + self.sway[0] - swing * 0.17, -0.17 + by + self.sway[1] - dip * 0.25 + swing * 0.05,
                         -0.36 - swing * 0.08)
            glRotatef(12 + swing * 65, 0, 1, 0)
            glRotatef(16 - swing * 10, 1, 0, 0)
            glRotatef(-20 + swing * 35, 0, 0, 1)
            glScalef(0.55, 0.55, 0.55)
            box(-0.022, -0.030, -0.02, 0.022, 0.030, 0.13, (0.12, 0.10, 0.08))        # handle
            box(-0.040, -0.040, -0.035, 0.040, 0.040, -0.02, metal_d)                  # guard
            box(-0.006, -0.026, -0.27, 0.006, 0.026, -0.035, (0.80, 0.82, 0.86))       # blade
            box(-0.007, 0.010, -0.30, 0.007, 0.026, -0.25, (0.70, 0.72, 0.76))        # tip
            box(-0.045, -0.050, -0.01, 0.045, 0.045, 0.10, glove)                      # hand
            beam((0.0, -0.02, 0.08), (0.20, -0.45, 0.32), 0.08, 0.085, sleeve)
            glDisable(GL_LIGHTING)
            glDisable(GL_COLOR_MATERIAL)
            glEnable(GL_FOG)
            return

        kick = recoil * (2.2 if w == W_SNIPER else 1.0)
        glTranslatef(0.165 + bx + self.sway[0], -0.180 + by - rl * 0.05 + self.sway[1] - dip * 0.30,
                     -0.43 + kick * 0.035)
        glRotatef(kick * 4.0 + rl * 14 - dip * 25, 1, 0, 0)
        glRotatef(rl * -28, 0, 0, 1)
        glRotatef(4.5, 0, 1, 0)                                   # toe the muzzle in towards the centre
        glScalef(0.52, 0.52, 0.52)
        out = 0.0
        if me.reloading:                                          # mag out in the middle of the reload
            out = 0.22 * max(0.0, min(1.0, (min(p, 1 - p) - 0.15) * 4.0))
        if w == W_SNIPER:
            olive, olive_d = (0.26, 0.30, 0.20), (0.18, 0.21, 0.14)
            box(-0.030, -0.050, -0.36, 0.030, 0.030, 0.06, metal)                # receiver
            box(-0.034, -0.060, -0.70, 0.034, 0.020, -0.30, olive)               # fore-end
            box(-0.010, -0.020, -1.20, 0.010, 0.000, -0.68, metal_d)             # long barrel
            box(-0.016, -0.026, -1.24, 0.016, 0.006, -1.18, metal_d)             # muzzle
            box(-0.030, -0.080, 0.06, 0.030, 0.026, 0.34, olive)                 # stock
            box(-0.026, 0.026, 0.08, 0.026, 0.050, 0.26, olive_d)                # cheek rest
            box(-0.024, -0.175, -0.05, 0.024, -0.050, 0.005, olive_d)            # grip
            box(-0.020, -0.12 - out, -0.24, 0.020, -0.050 - out, -0.17, metal_d) # short magazine
            box(-0.026, 0.040, -0.34, 0.026, 0.092, 0.00, (0.07, 0.07, 0.08))    # scope tube
            box(-0.036, 0.030, -0.40, 0.036, 0.102, -0.33, (0.07, 0.07, 0.08))   # objective bell
            box(-0.032, 0.034, -0.02, 0.032, 0.098, 0.04, (0.07, 0.07, 0.08))    # eyepiece
            box(-0.010, 0.000, -0.24, 0.010, 0.040, -0.20, metal_d)              # scope mount
            box(-0.010, 0.000, -0.10, 0.010, 0.040, -0.06, metal_d)
            bolt = 0.0
            if 0 < me.fire_cd < wd["interval"] - 0.15:                 # bolt cycles after the shot
                bolt = math.sin(math.pi * (1 - me.fire_cd / (wd["interval"] - 0.15)))
            box(0.030, -0.010, -0.10 + bolt * 0.10, 0.075, 0.015, -0.07 + bolt * 0.10, metal)  # bolt handle
        else:
            box(-0.032, -0.055, -0.32, 0.032, 0.035, 0.06, metal)           # receiver
            box(-0.034, -0.040, -0.20, 0.034, 0.010, -0.08, metal_d)        # ejection port side plate
            box(-0.012, 0.035, -0.30, 0.012, 0.050, 0.02, metal_d)          # top rail
            box(-0.016, 0.050, -0.02, 0.016, 0.085, 0.02, metal_d)          # rear sight
            box(-0.030, -0.050, -0.60, 0.030, 0.028, -0.32, poly)           # handguard
            box(-0.032, -0.030, -0.58, 0.032, 0.010, -0.36, mul(poly, 0.8)) # handguard grooves
            box(-0.011, -0.022, -0.84, 0.011, 0.000, -0.60, metal_d)        # barrel
            box(-0.018, -0.031, -0.88, 0.018, 0.009, -0.83, metal_d)        # muzzle brake
            box(-0.006, 0.000, -0.59, 0.006, 0.070, -0.57, metal_d)         # front sight post
            box(-0.026, -0.070, 0.06, 0.026, 0.028, 0.30, poly)             # stock
            box(-0.024, -0.175, -0.05, 0.024, -0.055, 0.005, poly)          # pistol grip
            box(-0.024, -0.215 - out, -0.26, 0.024, -0.055 - out, -0.165, metal_d)  # magazine

        # hands and forearms; elbows sit below/behind the screen edge so arms run off-screen
        box(-0.036, -0.155, -0.070, 0.040, -0.075, 0.012, glove)        # right hand on the grip
        beam((0.0, -0.13, -0.01), (0.30, -0.62, 0.42), 0.075, 0.080, sleeve)
        beam((0.0, -0.13, -0.01), (0.05, -0.20, 0.07), 0.085, 0.090, glove)   # wrist/cuff
        if not me.healing:
            if me.reloading:
                hand = (-0.02, -0.20 - out * 0.9, -0.22)                  # left hand on the magazine
            else:
                hand = (-0.01, -0.075, -0.47 if w == W_RIFLE else -0.52)  # left hand under the handguard
            hx, hy, hz = hand
            box(hx - 0.040, hy - 0.035, hz - 0.055, hx + 0.040, hy + 0.030, hz + 0.055, glove)
            beam((hx, hy - 0.02, hz + 0.02), (-0.46, -0.58, 0.05), 0.075, 0.080, sleeve)

        glDisable(GL_LIGHTING)
        glDisable(GL_COLOR_MATERIAL)
        if recoil > 0.55:
            if w == W_SNIPER:
                self.view_flash(0.0, -0.010, -1.26, 0.11 * recoil)
            else:
                self.view_flash(0.0, -0.011, -0.92, 0.075 * recoil)
        glEnable(GL_FOG)

    def view_flash(self, x, y, z, size):
        glDepthMask(GL_FALSE)
        glDisable(GL_CULL_FACE)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        rot = self.rng.random() * math.pi
        glBegin(GL_TRIANGLES)
        for k in range(6):
            a = rot + 2 * math.pi * k / 6
            for b in (a - 0.3, a + 0.3):
                glColor4f(1.0, 0.95, 0.75, 1.0)
                glVertex3f(x, y, z)
                glColor4f(1.0, 0.55, 0.1, 0.0)
                glVertex3f(x + math.cos(a) * size * 2.2, y + math.sin(a) * size * 2.2, z)
                glVertex3f(x + math.cos(b) * size * 0.7, y + math.sin(b) * size * 0.7, z)
        glEnd()
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glEnable(GL_CULL_FACE)
        glDepthMask(GL_TRUE)

# ------------------------------------------------------------------ window / input
def open_window(W, H, fullscreen, msaa):
    flags = DOUBLEBUF | OPENGL | (pygame.FULLSCREEN if fullscreen else 0)
    if msaa:
        try:
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 1)
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, msaa)
            return pygame.display.set_mode((W, H), flags, vsync=1)
        except Exception:
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 0)
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, 0)
    try:
        return pygame.display.set_mode((W, H), flags, vsync=1)
    except Exception:
        return pygame.display.set_mode((W, H), flags)


class MouseLook:
    """Unlimited mouse turning.

    Why turning used to stop: the window grabbed the mouse BEFORE hiding the cursor.
    With the cursor still visible at grab time SDL does not switch to relative mouse
    mode, so the pointer is merely confined to the window and stops at its edge.
    Fix: hide the cursor first, then grab (-> relative mode). pygame-ce also gets an
    explicit set_relative_mode(). As a fallback for desktops that still refuse relative
    mode, the pointer is warped back to the centre whenever it nears an edge, and the
    synthetic motion event from that warp is ignored. Arrow keys also turn."""

    MARGIN = 80

    def __init__(self, W, H):
        self.W, self.H = W, H
        self.center = (W // 2, H // 2)
        self.grabbed = False
        self.skip_frames = 0          # >0: ignore one motion event landing exactly on the centre
        self.rearm = False            # re-apply the grab on the next click (after focus changes)

    def grab(self, on):
        pygame.mouse.set_visible(not on)          # hide FIRST ...
        pygame.event.set_grab(on)                 # ... then grab -> SDL relative mode
        if hasattr(pygame.mouse, "set_relative_mode"):   # pygame-ce: ask for it directly
            try:
                pygame.mouse.set_relative_mode(on)
            except Exception:
                pass
        if on:
            self.warp()
        pygame.mouse.get_rel()
        self.grabbed = on
        self.rearm = False
        return on

    def warp(self):
        try:
            pygame.mouse.set_pos(self.center)
            self.skip_frames = 3
        except Exception:
            pass

    def motion(self, ev):
        """Returns (dx, dy) to apply for a MOUSEMOTION event."""
        if not self.grabbed:
            return 0, 0
        if self.skip_frames and abs(ev.pos[0] - self.center[0]) <= 1 and abs(ev.pos[1] - self.center[1]) <= 1:
            self.skip_frames = 0                  # this is the event our own warp produced
            return 0, 0
        return ev.rel

    def end_frame(self):
        """Re-centre the pointer if it drifted near the window edge."""
        if not self.grabbed:
            return
        if self.skip_frames:
            self.skip_frames -= 1
        x, y = pygame.mouse.get_pos()
        m = self.MARGIN
        if x < m or y < m or x > self.W - m or y > self.H - m:
            self.warp()




# ====================================================================== multi-player renderer
def color_of(pid: int):
    return PALETTE[pid % len(PALETTE)]


def rgb255(c, k=1.0):
    return tuple(max(0, min(255, int(v * 255 * k))) for v in c)


class Renderer(RendererBase):
    def __init__(self, W, H, fov=75.0, seed=7):
        super().__init__(W, H, fov, seed)
        self.anim = {}                 # pid -> dict(last, speed, phase, tracer, shot_dist)
        self.base_fov = fov
        self.cam = (0.0, 0.0, 0.0)
        self.cam_yaw = self.cam_pitch = 0.0

    # ---------------------------------------------------------- 3D -> screen (for name tags)
    def to_screen(self, x, y, z):
        cx, cy, cz = self.cam
        px, py, pz = x - cx, y - cy, z - cz
        cyw, syw = math.cos(self.cam_yaw), math.sin(self.cam_yaw)
        x1 = cyw * px + syw * pz
        z1 = -syw * px + cyw * pz
        cp, sp = math.cos(-self.cam_pitch), math.sin(-self.cam_pitch)
        y2 = cp * py - sp * z1
        z2 = sp * py + cp * z1
        if z2 > -0.1:
            return None
        k = self.H / (2.0 * math.tan(math.radians(self.fov) / 2.0)) / -z2
        return self.W / 2 + x1 * k, self.H / 2 + y2 * k, -z2

    # ---------------------------------------------------------- other players
    def draw_player(self, v, col, dead, flash, firing, phase, speed, ghost=False):
        team = col
        if ghost:
            g = 0.55 + 0.15 * math.sin(self.clock * 9.0)
            team = (g * 0.8, g * 0.85, g)
        if flash > 0:
            team = (1.0, 0.85, 0.85)
        dark = (0.13, 0.14, 0.16)
        cloth = (0.24, 0.26, 0.22) if not ghost else (0.4, 0.42, 0.48)
        skin = (0.86, 0.68, 0.52) if not ghost else (0.6, 0.62, 0.7)
        g = floor_at(v.x, v.z)
        airborne = g != INF and v.y > g + 0.08
        self.draw_blob_shadow(v.x, v.z, 0.45 if not airborne else 0.35)
        glEnable(GL_LIGHTING)
        glEnable(GL_COLOR_MATERIAL)
        self.set_sun(in_sun_shadow(v.x, v.y + 1.2, v.z))
        glPushMatrix()
        glTranslatef(v.x, v.y, v.z)
        glRotatef(-math.degrees(v.yaw), 0, 1, 0)
        if dead:
            glTranslatef(0, 0.18, 0)
            glRotatef(-90, 1, 0, 0)
            team = mul(team, 0.45)
        swing = 0.0 if dead or airborne else math.sin(phase) * min(1.0, speed / 4.0) * 35
        for side, sgn in ((-0.11, 1), (0.11, -1)):
            glPushMatrix()
            glTranslatef(side, 0.86, 0)
            glRotatef(swing * sgn, 1, 0, 0)
            cbox(0, -0.40, 0, 0.17, 0.80, 0.20, cloth)
            cbox(0, -0.80, -0.04, 0.19, 0.14, 0.30, dark)
            glPopMatrix()
        cbox(0, 1.16, 0, 0.50, 0.62, 0.28, mul(team, 0.85))
        cbox(0, 1.20, -0.01, 0.44, 0.44, 0.32, team)
        cbox(0, 0.88, 0, 0.48, 0.07, 0.30, dark)
        cbox(0, 1.30, -0.165, 0.30, 0.10, 0.03, mul(team, 0.6))
        cbox(0, 1.60, 0, 0.24, 0.26, 0.24, skin)
        cbox(0, 1.73, 0.01, 0.30, 0.12, 0.30, mul(team, 0.7))
        cbox(0, 1.665, 0.12, 0.30, 0.10, 0.06, mul(team, 0.7))
        cbox(0, 1.63, -0.125, 0.22, 0.06, 0.02, (0.05, 0.06, 0.08))
        glPushMatrix()
        glTranslatef(0, 1.38, 0)
        glRotatef(math.degrees(v.pitch), 1, 0, 0)
        weapon = getattr(v, "weapon", W_RIFLE)
        if weapon == W_KNIFE:
            cbox(-0.24, -0.20, -0.02, 0.13, 0.13, 0.30, mul(team, 0.85))        # arms low, knife forward
            cbox(0.28, -0.10, -0.14, 0.13, 0.13, 0.34, mul(team, 0.85))
            cbox(0.28, -0.12, -0.34, 0.10, 0.10, 0.10, skin)
            cbox(0.28, -0.10, -0.43, 0.04, 0.05, 0.10, (0.10, 0.08, 0.06))      # handle
            cbox(0.28, -0.09, -0.58, 0.012, 0.05, 0.22, (0.78, 0.80, 0.84))     # blade
        else:
            cbox(-0.30, -0.08, -0.12, 0.13, 0.13, 0.36, mul(team, 0.85))
            cbox(0.30, -0.08, -0.06, 0.13, 0.13, 0.30, mul(team, 0.85))
            cbox(-0.16, -0.10, -0.38, 0.10, 0.10, 0.12, skin)
            cbox(0.18, -0.12, -0.20, 0.10, 0.10, 0.10, skin)
            if weapon == W_SNIPER:
                cbox(0.06, -0.06, -0.42, 0.07, 0.09, 0.80, (0.20, 0.24, 0.18))  # long olive stock + body
                cbox(0.06, -0.05, -1.05, 0.03, 0.03, 0.50, (0.08, 0.08, 0.08))  # long barrel
                cbox(0.06, 0.04, -0.40, 0.06, 0.06, 0.30, (0.06, 0.06, 0.07))   # scope
                cbox(0.06, 0.04, -0.57, 0.075, 0.075, 0.05, (0.06, 0.06, 0.07))
            else:
                cbox(0.06, -0.06, -0.40, 0.08, 0.10, 0.70, dark)
                cbox(0.06, -0.05, -0.85, 0.035, 0.035, 0.22, (0.08, 0.08, 0.08))
                cbox(0.06, -0.15, -0.30, 0.05, 0.16, 0.08, (0.10, 0.10, 0.10))
        glPopMatrix()
        if getattr(v, "healing", False) and not dead:                          # medkit in the left hand
            cbox(-0.30, 1.05, -0.25, 0.22, 0.16, 0.12, (0.92, 0.92, 0.90))
            cbox(-0.30, 1.05, -0.315, 0.12, 0.04, 0.01, (0.85, 0.10, 0.10))
            cbox(-0.30, 1.05, -0.315, 0.04, 0.12, 0.01, (0.85, 0.10, 0.10))
        glPopMatrix()
        glDisable(GL_LIGHTING)
        glDisable(GL_COLOR_MATERIAL)
        if getattr(v, "healing", False) and not dead:
            self.plus_sprite(v.x, v.y + 2.35 + 0.05 * math.sin(self.clock * 6), v.z, 0.16)
        if weapon == W_KNIFE:
            firing = False
        if firing and not dead:
            d = look_dir(v.yaw, v.pitch)
            rx, rz = math.cos(v.yaw), math.sin(v.yaw)
            self.flash_sprite(v.x + rx * 0.06 + d[0] * 0.95, v.y + 1.32 + d[1] * 0.95,
                              v.z + rz * 0.06 + d[2] * 0.95, 0.35)
        if not dead:
            self.billboard_bar(v.x, v.y + 2.08, v.z, max(0.0, v.hp / MAX_HP), col)

    def draw_tracers(self, node, others):
        glDisable(GL_FOG)
        glDepthMask(GL_FALSE)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        glLineWidth(2.5)
        glBegin(GL_LINES)
        mz = self.muzzle_world(node.me)
        for (_s, e, ttl) in node.tracers:
            a = min(1.0, ttl / 0.06)
            t0, t1 = (1 - a) * 0.6, (1 - a) * 0.6 + 0.4
            if ttl > 0.06:                                     # sniper: the whole line, fading
                t0, t1 = 0.0, 1.0
            glColor4f(1.0, 0.85, 0.45, 0.0)
            glVertex3f(*(mz[i] + (e[i] - mz[i]) * t0 for i in range(3)))
            glColor4f(1.0, 0.95, 0.7, 0.9)
            glVertex3f(*(mz[i] + (e[i] - mz[i]) * t1 for i in range(3)))
        for v in others:
            an = self.anim.get(v.pid)
            if an is None or not v.alive or v.tracer_t <= 0 or v.weapon == W_KNIFE:
                continue
            d = look_dir(v.yaw, v.pitch)
            rx, rz = math.cos(v.yaw), math.sin(v.yaw)
            ex, ey, ez = v.x + rx * 0.06 + d[0] * 0.95, v.y + 1.32 + d[1] * 0.95, v.z + rz * 0.06 + d[2] * 0.95
            dist = an["shot_dist"]
            glColor4f(1.0, 0.6, 0.3, 0.0)
            glVertex3f(ex, ey, ez)
            glColor4f(1.0, 0.75, 0.4, 0.85)
            glVertex3f(ex + d[0] * dist, ey + d[1] * dist, ez + d[2] * dist)
        glEnd()
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDepthMask(GL_TRUE)
        glEnable(GL_FOG)

    # ---------------------------------------------------------- the 3D scene
    def scene(self, eye, yaw, pitch, dt, recoil=0.0, shake=0.0):
        self.clock += dt
        self.cam = eye
        self.cam_yaw, self.cam_pitch = yaw, pitch
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        self.perspective(self.fov, 0.05, 200.0)
        glLoadIdentity()
        glRotatef(-math.degrees(pitch) - recoil * 0.6 + self.rng.uniform(-shake, shake), 1, 0, 0)
        glRotatef(math.degrees(yaw) + self.rng.uniform(-shake, shake), 0, 1, 0)
        self.draw_sky()
        glTranslatef(-eye[0], -eye[1], -eye[2])
        self.draw_clouds(eye[0], eye[2])
        glEnable(GL_FOG)
        glCallList(self.world)

    def draw_backdrop(self, dt):
        """Slowly circling view over the arena behind the menus."""
        self.fov = self.base_fov
        t = self.clock * 0.05
        cx, cz = COLS * CELL / 2, ROWS * CELL / 2
        ex, ez = cx + math.sin(t) * 13.0, cz + math.cos(t) * 13.0
        yaw = math.atan2(cx - ex, -(cz - ez))
        self.scene((ex, 7.0, ez), yaw, -0.32, dt)

    def draw_game(self, node, dt, recoil, look_dx=0.0, look_dy=0.0):
        me = node.me
        others = node.others()
        self.recoil_vis = recoil
        if self.last_me is not None and me.alive:
            sp = math.hypot(me.x - self.last_me[0], me.z - self.last_me[1]) / max(dt, 1e-4)
        else:
            sp = 0.0
        self.last_me = (me.x, me.z)
        target_amp = min(1.4, sp / 5.0) if me.on_ground else 0.0
        self.bob_amp += (target_amp - self.bob_amp) * min(1.0, dt * 10)
        self.bob_phase += dt * (6.0 + sp * 1.1) * (1 if sp > 0.2 else 0)
        self.sway[0] += (-look_dx * 0.0006 - self.sway[0]) * min(1.0, dt * 9)
        self.sway[1] += (look_dy * 0.0006 - self.sway[1]) * min(1.0, dt * 9)
        self.sway = [max(-0.04, min(0.04, v)) for v in self.sway]
        # per-player animation + their shots
        for v in others:
            an = self.anim.setdefault(v.pid, dict(last=(v.x, v.z), speed=0.0, phase=0.0, tracer=0.0, shot_dist=20.0))
            rsp = math.hypot(v.x - an["last"][0], v.z - an["last"][1]) / max(dt, 1e-4)
            an["last"] = (v.x, v.z)
            an["speed"] += (min(rsp, 12.0) - an["speed"]) * min(1.0, dt * 8)
            an["phase"] += dt * (2.0 + an["speed"] * 2.0) if an["speed"] > 0.3 else 0.0
            if v.alive and v.tracer_t > an["tracer"] and v.weapon != W_KNIFE:
                d = look_dir(v.yaw, v.pitch)
                ex, ey, ez = v.x, v.y + EYE_H, v.z
                kind, dist = wall_hit(ex, ey, ez, d, 60.0)
                an["shot_dist"] = dist
                if kind in ("wall", "floor"):
                    self.spawn_impact((ex + d[0] * dist, ey + d[1] * dist, ez + d[2] * dist), d, kind)
            an["tracer"] = v.tracer_t
        while node.shots:
            self.spawn_impact(*node.shots.popleft())
        self.update_effects(dt)
        ev = node.err_vis
        eye = (me.x + ev[0], me.y + ev[1] + EYE_H, me.z + ev[2])
        target_fov = SCOPE_FOV if me.scoped and me.alive else self.base_fov
        self.fov += (target_fov - self.fov) * min(1.0, dt * 18)
        if abs(self.fov - target_fov) < 0.3:
            self.fov = target_fov
        self.scene(eye, me.yaw, me.pitch, dt, recoil, node.damage_t * 2.5)
        self.draw_pickups(node.pickups)
        for v in others:
            self.draw_player(v, color_of(v.pid), not v.alive, v.hit_flash, v.tracer_t > 0,
                             self.anim[v.pid]["phase"], self.anim[v.pid]["speed"], ghost=v.lagging)
        self.draw_effects()
        self.draw_tracers(node, others)
        if me.alive and not (me.scoped and self.fov < self.base_fov * 0.6):
            self.draw_viewmodel(me, recoil, color_of(me.pid))

    # ---------------------------------------------------------- HUD
    def begin_2d(self):
        self.ortho()
        glDisable(GL_DEPTH_TEST)
        glDisable(GL_FOG)
        glDisable(GL_CULL_FACE)

    def end_2d(self):
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_FOG)
        glEnable(GL_CULL_FACE)

    def draw_hud(self, node, grabbed, show_scores):
        W, H, txt = self.W, self.H, self.txt
        self.begin_2d()
        me, now = node.me, node.now
        cx, cy = W / 2, H / 2
        mycol = color_of(me.pid)
        # vignette (+ red when hurt)
        hurt = min(1.0, node.damage_t / 0.4) if node.damage_t > 0 else 0.0
        low = 0.35 if me.alive and me.hp <= 30 else 0.0
        red = max(hurt, low * (0.6 + 0.4 * math.sin(now * 5)))
        edge = (0.5 * red, 0.0, 0.0)
        ea = 0.45 + 0.35 * red
        glBegin(GL_QUADS)
        for (ax, ay), (bx, by) in (((0, 0), (W, 0)), ((W, 0), (W, H)), ((W, H), (0, H)), ((0, H), (0, 0))):
            ix0, iy0 = ax + (cx - ax) * 0.3, ay + (cy - ay) * 0.3
            ix1, iy1 = bx + (cx - bx) * 0.3, by + (cy - by) * 0.3
            glColor4f(*edge, ea)
            glVertex2f(ax, ay); glVertex2f(bx, by)
            glColor4f(*edge, 0.0)
            glVertex2f(ix1, iy1); glVertex2f(ix0, iy0)
        glEnd()
        # name tags (only players in line of sight)
        eye = self.cam
        for v in node.others():
            head = (v.x, v.y + 2.35, v.z)
            p = self.to_screen(*head)
            if p is None or p[2] > 45 or not line_of_sight(eye, (v.x, v.y + 1.5, v.z)):
                continue
            size = 15 if p[2] < 15 else 13
            col = rgb255(color_of(v.pid), 1.25)
            label = v.name + ("  (host)" if v.is_host else "")
            txt.draw(p[0], p[1], label, col if v.alive else (170, 170, 170), size, align="center")
            if v.lagging:
                txt.draw(p[0], p[1] + 19, "LAGGING - shots don't count", (255, 190, 90), 12, align="center")
        if me.alive and me.scoped and self.fov < self.base_fov * 0.6:
            self.draw_scope_overlay()
        # crosshair + hit markers (white = my screen says hit, red = the host confirmed it)
        if me.alive:
            gap = 6 + 10 * min(1.0, self.bob_amp) + 9 * self.recoil_vis
            if me.weapon == W_SNIPER and not me.scoped:
                gap += 16                                     # hip fire: wide
            elif me.weapon == W_KNIFE:
                gap = 3
            glLineWidth(2)
            glBegin(GL_LINES)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                glColor4f(0, 0, 0, 0.6)
                glVertex2f(cx + dx * gap + 1, cy + dy * gap - 1)
                glVertex2f(cx + dx * (gap + 10) + 1, cy + dy * (gap + 10) - 1)
                glColor4f(1, 1, 1, 0.95)
                glVertex2f(cx + dx * gap, cy + dy * gap)
                glVertex2f(cx + dx * (gap + 10), cy + dy * (gap + 10))
            glEnd()
            rect(cx - 1, cy - 1, cx + 1, cy + 1, (1, 1, 1), 0.9)
            for t, c, r0, r1, w in ((node.hitmarker_t, (1.0, 1.0, 1.0), 8, 14, 2), (node.confirm_t, (1.0, 0.25, 0.2), 9, 19, 3)):
                if t > 0:
                    glLineWidth(w)
                    glColor4f(*c, min(1.0, t * 8))
                    glBegin(GL_LINES)
                    for dx, dy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
                        glVertex2f(cx + dx * r0, cy + dy * r0)
                        glVertex2f(cx + dx * r1, cy + dy * r1)
                    glEnd()
        # health panel
        rect(18, 18, 300, 74, (0.02, 0.03, 0.05), 0.55)
        rect(18, 72, 300, 74, mycol, 0.9)
        frac = max(0.0, me.hp / MAX_HP)
        hc = (0.25 + 0.7 * (1 - frac), 0.80 * frac + 0.12, 0.25)
        rect(92, 30, 288, 46, (0.0, 0.0, 0.0), 0.5)
        rect(92, 30, 92 + 196 * frac, 46, hc, 0.95)
        rect(92, 41, 92 + 196 * frac, 46, (1, 1, 1), 0.15)
        rect(36, 37, 64, 43, (1, 1, 1), 0.95)
        rect(47, 26, 53, 54, (1, 1, 1), 0.95)
        txt.draw(92, 48, f"{me.hp}", (255, 255, 255), 18)
        txt.draw(288, 48, "HEALTH", (190, 200, 215), 12, align="right")
        # medkits + heal timer (next to the health panel)
        rect(310, 18, 470, 74, (0.02, 0.03, 0.05), 0.55)
        rect(310, 72, 470, 74, mycol, 0.9)
        for k in range(MEDKIT_MAX):
            x = 322 + k * 30
            have = k < me.medkits
            rect(x, 48, x + 24, 66, (0.93, 0.93, 0.91) if have else (0.30, 0.30, 0.32), 0.95 if have else 0.5)
            if have:
                rect(x + 9, 50, x + 15, 64, (0.85, 0.1, 0.1), 1.0)
                rect(x + 4, 54, x + 20, 60, (0.85, 0.1, 0.1), 1.0)
        if me.healing:
            frac = 1.0 - me.heal_t / HEAL_TIME
            rect(322, 26, 458, 36, (0, 0, 0), 0.5)
            rect(322, 26, 322 + 136 * frac, 36, (0.3, 1.0, 0.4), 0.95)
            txt.draw(458, 52, f"HEALING {me.heal_t:.1f}s", (140, 255, 160), 12, align="right")
        elif me.heal_cd > 0:
            frac = 1.0 - me.heal_cd / HEAL_COOLDOWN
            rect(322, 26, 458, 36, (0, 0, 0), 0.5)
            rect(322, 26, 322 + 136 * frac, 36, (0.55, 0.6, 0.7), 0.8)
            txt.draw(458, 52, f"H in {me.heal_cd:.0f}s", (200, 205, 215), 12, align="right")
        else:
            txt.draw(458, 52, "H heal" if me.medkits else "no medkit", (140, 255, 160) if me.medkits else
                     (150, 155, 165), 12, align="right")
            txt.draw(322, 26, f"{me.medkits}/{MEDKIT_MAX} medkits", (190, 200, 215), 12)
        if me.alive and me.hp <= 50 and me.can_heal():
            txt.draw(cx, cy - 98, "H  to heal", (140, 255, 160), 18, align="center")
        # ammo panel
        wd = WEAPONS[me.weapon]
        rect(W - 300, 18, W - 18, 74, (0.02, 0.03, 0.05), 0.55)
        rect(W - 300, 72, W - 18, 74, mycol, 0.9)
        if me.weapon == W_KNIFE:
            txt.draw(W - 30, 38, "KNIFE", (255, 255, 255), 26, align="right")
            txt.draw(W - 288, 44, "MELEE  2.3 m", (190, 200, 215), 12)
        elif me.reloading:
            p = 1.0 - me.reload_t / wd["reload"]
            rect(W - 288, 26, W - 30, 34, (0.0, 0.0, 0.0), 0.5)
            rect(W - 288, 26, W - 288 + 258 * p, 34, (1.0, 0.8, 0.3), 0.95)
            txt.draw(W - 30, 40, "RELOADING", (255, 210, 90), 22, align="right")
        else:
            mag = wd["mag"]
            col = (255, 120, 90) if me.ammo <= max(1, mag // 6) else (255, 255, 255)
            w = txt.draw(W - 30, 42, f"/ {mag}", (170, 180, 195), 16, align="right")
            txt.draw(W - 36 - w, 38, f"{me.ammo}", col, 26, align="right")
            txt.draw(W - 288, 44, wd["name"].upper(), (190, 200, 215), 12)
            step = min(8.6, 160.0 / mag)
            for i in range(mag):
                x = W - 288 + i * step
                full = i < me.ammo
                rect(x, 26, x + step - 2.6, 36 if full else 28, (1.0, 0.82, 0.35) if full else (0.45, 0.45, 0.45),
                     0.95 if full else 0.6)
        if me.alive and me.mag_size and not me.reloading and me.ammo <= max(1, me.mag_size // 6):
            txt.draw(cx, cy - 70, "R  to reload", (255, 210, 90), 18, align="center")
        # weapon slots (bottom centre)
        x = cx - 165
        for slot, wid in enumerate((W_RIFLE, W_SNIPER, W_KNIFE)):
            on = me.weapon == wid
            rect(x, 18, x + 104, 46, (0.02, 0.03, 0.05), 0.70 if on else 0.40)
            if on:
                rect(x, 44, x + 104, 46, mycol, 1.0)
            ammo = "" if not WEAPONS[wid]["mag"] else f"  {me.mags[wid]}"
            txt.draw(x + 52, 26, f"{slot + 1} {WEAPONS[wid]['name']}{ammo}", (255, 255, 255) if on else (150, 155, 165),
                     13, align="center")
            x += 112
        if me.weapon == W_SNIPER and not me.scoped and me.alive:
            txt.draw(cx, 54, "right mouse: scope", (190, 200, 215), 12, align="center")
        # my score (top centre)
        rect(cx - 110, H - 52, cx + 110, H - 12, (0.02, 0.03, 0.05), 0.55)
        rect(cx - 110, H - 52, cx - 106, H - 12, mycol, 1.0)
        txt.draw(cx - 96, H - 43, me.name, rgb255(mycol, 1.3), 14)
        txt.draw(cx + 98, H - 47, f"{me.kills} K  {me.deaths} D", (255, 255, 255), 20, align="right")
        # network panel (top left)
        self.draw_net(node)
        self.draw_minimap(node)
        # kill feed (right, under the minimap)
        y = H - COLS * self.mm_scale() - 52
        for killer, victim, head, weapon, ttl in reversed(node.kill_feed):
            a = min(1.0, ttl)
            tag = {W_SNIPER: "[SNIPER] ", W_KNIFE: "[KNIFE] "}.get(weapon, "")
            s = f"{killer}  {tag}{'[HEADSHOT] ' if head else ''}->  {victim}"
            txt.draw(W - 22, y, s, rgb255((1.0, 0.9 * a + 0.1, 0.8 * a + 0.2)), 15, align="right")
            y -= 22
        # messages
        y = cy + 130
        for text, ttl in node.messages:
            txt.draw(cx, y, text, (255, 255, 255), 22, align="center")
            y -= 32
        if not me.alive and node.admitted:
            rect(0, 0, W, H, (0.15, 0, 0), 0.40)
            txt.draw(cx, cy + 10, "ELIMINATED", (255, 90, 90), 44, align="center")
            wait = "respawn in %.1f s" % max(0.0, me.dead_t) if me.dead_t > 0 else "waiting for the host to respawn you"
            txt.draw(cx, cy - 30, wait, (240, 240, 240), 20, align="center")
        if show_scores:
            self.draw_scoreboard(node)
        self.end_2d()

    def draw_net(self, node):
        txt, H = self.txt, self.H
        now = node.now
        lines = []
        if node.role == "host":
            n = len(node.world.players) - 1 if node.world else 0
            head = (f"HOST  ·  {n} client{'s' if n != 1 else ''}  ·  sending {node.current_rate():g}/s", (170, 255, 180))
            for pid, q in sorted(node.world.players.items()):
                if pid == node.node:
                    continue
                lag = node.world.lagging(q, now)
                ping = f"{q.rtt * 1000:.0f} ms" if q.rtt is not None else "-"
                col = (255, 190, 90) if lag else (215, 225, 240)
                lines.append((f"{q.name}: ping {ping}" + ("   LAGGING" if lag else ""), col))
        elif node.role == "client":
            ping = f"{node.rtt_host * 1000:.0f} ms" if node.rtt_host is not None else "-"
            hp = node.peers.get(node.host_id)
            rate = hp.pkt_rate(now) if hp else 0.0
            head = (f"client of {node.name_of(node.host_id)}  ·  ping {ping}  ·  {rate:.1f} pkt/s in", (170, 220, 255))
            lines.append((f"drawn {node.interp_delay() * 1000:.0f} ms behind the host  ·  corrections "
                          f"{node.stats['corrections']}", (200, 210, 225)))
            lines.append((f"sending {node.current_rate():.1f}/s (menu {node.rate:g}/s)", (200, 210, 225)))
        else:
            head = (node.role_text(), (255, 200, 120))
        if node.adaptive and node.role in ("host", "client") and node.rate_eff < node.rate - 0.05:
            lines.append((f"radio queue building up ({node.cc_queue * 1000:.0f} ms): rate lowered to "
                          f"{node.rate_eff:.1f}/s", (255, 200, 120)))
        rect(14, H - 36, 18, H - 14, tuple(c / 255 for c in head[1]), 1.0)
        txt.draw(26, H - 34, head[0], head[1], 14)
        y = H - 54
        for s, c in lines[:6]:
            txt.draw(26, y, s, c, 13)
            y -= 18

    def draw_scope_overlay(self):
        W, H = self.W, self.H
        cx, cy, R = W / 2, H / 2, H * 0.46
        glBegin(GL_TRIANGLE_STRIP)
        for i in range(65):
            a = 2 * math.pi * i / 64
            ca, sa = math.cos(a), math.sin(a)
            glColor4f(0, 0, 0, 1.0)
            glVertex2f(cx + ca * R, cy + sa * R)
            glVertex2f(cx + ca * W * 2, cy + sa * W * 2)
        glEnd()
        glBegin(GL_TRIANGLE_STRIP)                             # soft dark edge inside the tube
        for i in range(65):
            a = 2 * math.pi * i / 64
            ca, sa = math.cos(a), math.sin(a)
            glColor4f(0, 0, 0, 0.0)
            glVertex2f(cx + ca * R * 0.86, cy + sa * R * 0.86)
            glColor4f(0, 0, 0, 0.85)
            glVertex2f(cx + ca * R, cy + sa * R)
        glEnd()
        glLineWidth(1.5)
        glColor4f(0, 0, 0, 0.9)
        glBegin(GL_LINES)
        glVertex2f(cx - R, cy); glVertex2f(cx - 6, cy)
        glVertex2f(cx + 6, cy); glVertex2f(cx + R, cy)
        glVertex2f(cx, cy - R); glVertex2f(cx, cy - 6)
        glVertex2f(cx, cy + 6); glVertex2f(cx, cy + R)
        for k in range(1, 5):                                  # mil dots
            for sx, sy in ((1, 0), (-1, 0), (0, -1)):
                px, py = cx + sx * k * R * 0.16, cy + sy * k * R * 0.16
                glVertex2f(px - 4 * (sy != 0), py - 4 * (sx != 0)); glVertex2f(px + 4 * (sy != 0), py + 4 * (sx != 0))
        glEnd()
        glLineWidth(3)
        glBegin(GL_LINES)
        glVertex2f(cx - R, cy); glVertex2f(cx - R * 0.55, cy)
        glVertex2f(cx + R * 0.55, cy); glVertex2f(cx + R, cy)
        glVertex2f(cx, cy - R); glVertex2f(cx, cy - R * 0.55)
        glEnd()
        rect(cx - 1.5, cy - 1.5, cx + 1.5, cy + 1.5, (0.9, 0.1, 0.1), 1.0)

    def mm_scale(self):
        return max(5.0, min(9.0, self.H / 80.0))

    def draw_minimap(self, node):
        W, H = self.W, self.H
        sc = self.mm_scale()
        size = COLS * sc
        x0, y0 = W - size - 18, H - size - 18
        rect(x0 - 6, y0 - 6, x0 + size + 6, y0 + size + 6, (0.02, 0.03, 0.05), 0.55)
        glBegin(GL_QUADS)
        for r in range(ROWS):
            for c in range(COLS):
                info = cell_info(r, c)
                X, Y = x0 + c * sc, y0 + size - (r + 1) * sc
                if info[0] == "wall":
                    glColor4f(0.80, 0.74, 0.62, 0.85)
                elif info[0] == "flat" and info[1] > 0:           # roofs / crates: brighter = higher
                    k = info[1] / 3.0
                    glColor4f(0.30 + 0.25 * k, 0.42 + 0.25 * k, 0.60 + 0.25 * k, 0.85)
                elif info[0] == "ramp":
                    glColor4f(0.40, 0.55, 0.75, 0.75)
                else:
                    continue
                glVertex2f(X, Y); glVertex2f(X + sc, Y); glVertex2f(X + sc, Y + sc); glVertex2f(X, Y + sc)
        glEnd()
        for x, _y, z, _k in node.pickups:                      # medkits: small green crosses
            px, py = x0 + x / CELL * sc, y0 + size - z / CELL * sc
            rect(px - 3.5, py - 1.2, px + 3.5, py + 1.2, (0.3, 1.0, 0.4), 0.95)
            rect(px - 1.2, py - 3.5, px + 1.2, py + 3.5, (0.3, 1.0, 0.4), 0.95)

        def arrow(x, z, yaw, col):
            px, py = x0 + x / CELL * sc, y0 + size - z / CELL * sc
            dx, dy = math.sin(yaw), math.cos(yaw)
            glColor4f(*col, 1)
            glBegin(GL_TRIANGLES)
            glVertex2f(px + dx * 8, py + dy * 8)
            glVertex2f(px - dx * 4 + dy * 5, py - dy * 4 - dx * 5)
            glVertex2f(px - dx * 4 - dy * 5, py - dy * 4 + dx * 5)
            glEnd()
        for v in node.others():
            if v.alive:
                arrow(v.x, v.z, v.yaw, (0.6, 0.6, 0.65) if v.lagging else color_of(v.pid))
        if node.me.alive:
            arrow(node.me.x, node.me.z, node.me.yaw, (1.0, 1.0, 1.0))

    def draw_scoreboard(self, node):
        W, H, txt = self.W, self.H, self.txt
        rows = node.scoreboard()
        w, h = 560, 70 + 30 * len(rows)
        x0, y0 = W / 2 - w / 2, H / 2 - h / 2
        rect(x0, y0, x0 + w, y0 + h, (0.02, 0.03, 0.06), 0.82)
        txt.draw(x0 + 20, y0 + h - 34, "SCOREBOARD", (255, 255, 255), 20)
        txt.draw(x0 + w - 20, y0 + h - 32, node.arena_summary_short(), (180, 190, 210), 13, align="right")
        y = y0 + h - 62
        for col, label in ((300, "K"), (360, "D"), (440, "PING")):
            txt.draw(x0 + col, y, label, (170, 180, 200), 13, align="center")
        for pid, name, k, d, ping, is_host, is_me, lag in rows:
            y -= 30
            if is_me:
                rect(x0 + 8, y - 6, x0 + w - 8, y + 20, (1, 1, 1), 0.08)
            rect(x0 + 18, y - 2, x0 + 24, y + 16, color_of(pid), 1.0)
            tag = ("  HOST" if is_host else "") + ("  (you)" if is_me else "") + ("  LAG" if lag else "")
            txt.draw(x0 + 34, y, f"{name}{tag}", (255, 255, 255), 16)
            txt.draw(x0 + 300, y, str(k), (255, 255, 255), 16, align="center")
            txt.draw(x0 + 360, y, str(d), (255, 255, 255), 16, align="center")
            txt.draw(x0 + 440, y, "-" if ping is None else f"{ping * 1000:.0f} ms", (210, 220, 235), 14, align="center")


# ====================================================================== menus (immediate-mode buttons)
class UI:
    def __init__(self, rend):
        self.r = rend
        self.hits = []                 # (x0, y0, x1, y1, id) of this frame
        self.prev = []
        self.mouse = (0, 0)
        self.clicked = None

    def begin(self, mouse_gl, click):
        self.prev, self.hits = self.hits, []
        self.mouse = mouse_gl
        self.clicked = None
        if click is not None:
            for x0, y0, x1, y1, ident in self.prev:
                if x0 <= click[0] <= x1 and y0 <= click[1] <= y1:
                    self.clicked = ident

    def hot(self, x0, y0, x1, y1):
        return x0 <= self.mouse[0] <= x1 and y0 <= self.mouse[1] <= y1

    def button(self, ident, x0, y0, x1, y1, label, primary=False, enabled=True, size=18):
        hot = enabled and self.hot(x0, y0, x1, y1)
        base = (0.20, 0.45, 0.85) if primary else (0.16, 0.18, 0.24)
        if not enabled:
            base = (0.12, 0.13, 0.16)
        rect(x0, y0, x1, y1, tuple(min(1.0, c * (1.35 if hot else 1.0)) for c in base), 0.92)
        rect(x0, y0, x1, y0 + 2, (0, 0, 0), 0.4)
        col = (255, 255, 255) if enabled else (120, 125, 135)
        self.r.txt.draw((x0 + x1) / 2, (y0 + y1) / 2 - size * 0.45, label, col, size, align="center")
        if enabled:
            self.hits.append((x0, y0, x1, y1, ident))
        return self.clicked == ident and enabled


class Settings:
    """Menu settings; saved in ~/.cdp_game.json. A value given on the command line (by the flowgraph) wins
    whenever it differs from the one given last time, otherwise the value last chosen in the menu is used."""

    PATH = os.path.expanduser("~/.cdp_game.json")           # v6.8; v6.9 keeps its own: ~/.cdp_game_v69.json
    KEYS = ("name", "rate", "max_players", "max_lag", "elect_random", "arq", "bots")

    def __init__(self, cli: dict, path=None, arq_default=True):
        if path:
            self.PATH = os.path.expanduser(path)
        self.v = dict(name="Player", rate=DEFAULT_RATE, max_players=DEFAULT_MAX_PLAYERS, max_lag=DEFAULT_MAX_LAG,
                      elect_random=False, arq=arq_default, bots=2)
        saved = {}
        try:
            with open(self.PATH) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            pass
        old_cli = saved.get("cli", {})
        for k in self.KEYS:
            if k in saved.get("settings", {}):
                self.v[k] = saved["settings"][k]
            if cli.get(k) is not None and cli.get(k) != old_cli.get(k):
                self.v[k] = cli[k]
        self.cli = {k: v for k, v in cli.items() if v is not None}

    def save(self):
        try:
            with open(self.PATH, "w") as f:
                json.dump({"settings": self.v, "cli": self.cli}, f, indent=1)
        except OSError:
            pass

    def __getitem__(self, k):
        return self.v[k]

    def __setitem__(self, k, val):
        self.v[k] = val


def channel_hint(players, rate, arq, stack="v69"):
    """Rough channel load for the menu: frames/s all players put on the air together (with a full arena)."""
    frames = players * rate + players / 3.0
    acks = (players - 1) * rate if arq else 0.0
    txt = f"full arena ({players} players at {rate:g}/s): ~{frames:.0f} frames/s" + (f" + {acks:.0f} Block ACKs/s" if arq else "")
    load = frames + acks
    if stack == "v69" and arq and players > 2:
        return txt, "v6.9: reliable inputs queue up with 3+ players - use datagrams", (255, 170, 110)
    if stack == "v69" and not arq:                # datagrams: no ACKs, up to 4 frames per channel access
        if load > 40:
            return txt, "too much for the v6.9 channel (~40 datagrams/s): lower the rate or the player count", (255, 170, 110)
        if load > 30:
            return txt, "busy channel: the game lowers its rate by itself when the radio queue builds up", (255, 220, 140)
        return txt, "fits the channel (datagrams)", (170, 240, 180)
    if load > 18:
        return txt, (f"too much for the {stack[:2]}.{stack[2:]} channel (it carries ~15-18/s): lower the rate or the "
                     f"player count"), (255, 170, 110)
    if load > 12:
        return txt, "busy channel: expect some lag", (255, 220, 140)
    return txt, "fits the channel", (170, 240, 180)


class App:
    def __init__(self, a, rend: Renderer, W, H):
        self.a, self.r, self.W, self.H = a, rend, W, H
        self.ui = UI(rend)
        cli = dict(rate=a.rate, max_players=a.max_players, max_lag=a.max_lag, arq=a.arq, name=a.name)
        v69 = a.stack == "v69"
        self.s = Settings(cli, "~/.cdp_game_v69.json" if v69 else None, arq_default=not v69)
        self.peers_expected = [int(x) for x in os.environ.get("CDP_PEERS", "").replace(" ", "").split(",")
                               if x.isdigit()]
        self.ctl_port = os.environ.get("CDP_CTL_PORT", "").strip()
        self.ctl_msg = ""
        self.node_id = a.node if a.node is not None else (int(os.environ["CDP_MY_ID"])
                                                          if os.environ.get("CDP_MY_ID", "").strip().isdigit() else None)
        self.screen = "menu"
        self.node: Optional[Node] = None
        self.practice: Optional[Practice] = None
        self.paused = False
        self.editing = False
        self.error = ""
        self.info = ""

    # ---------------------------------------------------------- sessions
    def start_online(self):
        if self.node_id is None:
            self.error = "This node's ID is unknown: export CDP_MY_ID=<id> (the flowgraph's MY_ID) or use --node"
            return
        try:
            if self.a.stack == "v69":
                link = BridgeLink(self.node_id, self.a.udp_port, self.a.udp_out_port, self.a.arq_port)
            else:
                link = RadioLink(self.node_id, self.a.tx_port, self.a.rx_port)
        except OSError as e:
            self.error = str(e)
            return
        s = self.s
        self.node = Node(self.node_id, link, name=s["name"], rate=s["rate"], max_players=s["max_players"],
                         max_lag=s["max_lag"], arq=s["arq"], elect_random=s["elect_random"])
        self.practice = None
        self.screen = "lobby"
        self.error = ""
        s.save()

    def start_practice(self):
        s = self.s
        self.practice = Practice(my_node=0, n_bots=int(s["bots"]), rate=s["rate"], max_players=max(s["max_players"], int(s["bots"]) + 1),
                                 max_lag=s["max_lag"], name=s["name"])
        self.node = self.practice.node
        self.node.join_game()
        self.screen = "lobby"
        self.error = ""
        s.save()

    def to_menu(self):
        if self.node is not None:
            if self.practice is None:
                self.node.shutdown()
                self.node.link.close()
            self.node = None
            self.practice = None
        self.screen = "menu"
        self.paused = False

    def close_everything(self) -> bool:
        """Ask cdp_daemon.py to stop everything (game, flowgraph, GRC, chat) via its control port."""
        import socket as _socket
        try:
            with _socket.create_connection(("127.0.0.1", int(self.ctl_port or "5300")), timeout=2) as c:
                c.sendall(b"SHUTDOWN\n")
            return True
        except (OSError, ValueError) as e:
            self.error = f"cdp_daemon.py not reachable on port {self.ctl_port or 5300} ({e}) - only the game closes"
            return False

    def stack_text(self):
        if self.a.stack == "v69":
            return f"v6.9 radio, udp {self.a.udp_port}/{self.a.udp_out_port}"
        return f"v6.8 radio, udp {self.a.tx_port}/{self.a.rx_port}"

    def step_net(self, dt, *a, **kw):
        if self.practice is not None:
            self.practice.step(dt, *a, **kw)
        elif self.node is not None:
            self.node.step(dt, *a, **kw)

    # ---------------------------------------------------------- text entry for the name
    def key(self, ev):
        if not self.editing:
            return False
        if ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_TAB, pygame.K_ESCAPE):
            self.editing = False
            self.s["name"] = self.s["name"].strip() or "Player"
        elif ev.key == pygame.K_BACKSPACE:
            self.s["name"] = self.s["name"][:-1]
        else:
            ch = getattr(ev, "unicode", "")
            if ch and 32 <= ord(ch) < 127 and len(self.s["name"]) < 8:
                self.s["name"] += ch
        return True

    # ---------------------------------------------------------- screens
    def panel(self, w, h):
        x0, y0 = self.W / 2 - w / 2, self.H / 2 - h / 2
        rect(x0, y0, x0 + w, y0 + h, (0.03, 0.04, 0.07), 0.80)
        rect(x0, y0 + h - 3, x0 + w, y0 + h, (0.25, 0.55, 0.95), 0.9)
        return x0, y0

    def draw_menu(self):
        r, ui, s, txt = self.r, self.ui, self.s, self.r.txt
        x0, y0 = self.panel(720, 620)
        top = y0 + 620
        txt.draw(self.W / 2, top - 62, "RADIO ARENA", (255, 255, 255), 40, align="center")
        node = f"node {self.node_id}" if self.node_id is not None else "node ID not set (CDP_MY_ID)"
        opp = (f"  ·  nodes heard {', '.join(map(str, self.peers_expected))}" if self.peers_expected else "")
        txt.draw(self.W / 2, top - 92, f"{node}{opp}  ·  {self.stack_text()}  ·  host-authoritative",
                 (170, 185, 210), 14, align="center")
        lx, vx = x0 + 60, x0 + 330
        y = top - 150

        def row(label):
            txt.draw(lx, y + 8, label, (210, 220, 235), 17)

        def stepper(key, label, val, dec, inc):
            nonlocal y
            row(label)
            if ui.button(key + "-", vx, y, vx + 44, y + 34, "-"):
                dec()
            txt.draw(vx + 120, y + 8, val, (255, 255, 255), 18, align="center")
            if ui.button(key + "+", vx + 196, y, vx + 240, y + 34, "+"):
                inc()
            y -= 48

        row("Name")
        field = (vx, y, vx + 240, y + 34)
        rect(*field, (0.10, 0.12, 0.16) if not self.editing else (0.16, 0.22, 0.32), 0.95)
        cursor = "|" if self.editing and int(self.r.clock * 2) % 2 == 0 else ""
        txt.draw(vx + 12, y + 8, s["name"] + cursor, (255, 255, 255), 18)
        ui.hits.append((*field, "name"))
        if ui.clicked == "name":
            self.editing = True
        y -= 48
        stepper("rate", "Packet rate", f"{s['rate']:g} /s",
                lambda: s.__setitem__("rate", max(MIN_RATE, round(s["rate"] - (0.5 if s["rate"] <= 3 else 1), 1))),
                lambda: s.__setitem__("rate", min(MAX_RATE, round(s["rate"] + (0.5 if s["rate"] < 3 else 1), 1))))
        stepper("maxp", "Max players", f"{s['max_players']}",
                lambda: s.__setitem__("max_players", max(2, s["max_players"] - 1)),
                lambda: s.__setitem__("max_players", min(MAX_PLAYERS_LIMIT, s["max_players"] + 1)))
        stepper("lag", "Lag limit", f"{s['max_lag'] * 1000:.0f} ms",
                lambda: s.__setitem__("max_lag", max(0.3, round(s["max_lag"] - 0.1, 1))),
                lambda: s.__setitem__("max_lag", min(3.0, round(s["max_lag"] + 0.1, 1))))
        row("Host election")
        if ui.button("elect", vx, y, vx + 240, y + 34, "Random" if s["elect_random"] else "Lowest ping"):
            s["elect_random"] = not s["elect_random"]
        y -= 48
        row("Radio")
        if self.a.stack == "v69":
            lab = "Inputs reliable (ARQ)" if s["arq"] else "Datagrams (ARQ bypass)"
        else:
            lab = "ARQ on (inputs)" if s["arq"] else "ARQ bypass"
        if ui.button("arq", vx, y, vx + 240, y + 34, lab):
            s["arq"] = not s["arq"]
        y -= 40
        hint, verdict, hc = channel_hint(s["max_players"], s["rate"], s["arq"], self.a.stack)
        txt.draw(self.W / 2, y + 4, hint, (190, 200, 220), 13, align="center")
        txt.draw(self.W / 2, y - 14, verdict, hc, 13, align="center")
        y -= 66
        if ui.button("play", x0 + 60, y, x0 + 300, y + 44, "Play online", primary=True, size=20):
            self.start_online()
        if ui.button("practice", x0 + 320, y, x0 + 540, y + 44, f"Practice vs {s['bots']} bot{'s' if s['bots'] != 1 else ''}"):
            self.start_practice()
        if ui.button("bots_up", x0 + 548, y + 22, x0 + 580, y + 44, "+", size=14):
            s["bots"] = min(7, s["bots"] + 1)
        if ui.button("bots_down", x0 + 548, y, x0 + 580, y + 20, "-", size=14):
            s["bots"] = max(1, s["bots"] - 1)
        if ui.button("quit", x0 + 590, y, x0 + 660, y + 44, "Quit"):
            return "quit"
        y -= 58
        if self.ctl_port and ui.button("closeall", x0 + 60, y, x0 + 660, y + 40,
                                       "Close everything (game, flowgraph, GNU Radio Companion, chat)", size=16):
            if self.close_everything():
                return "quit"
        if self.error:
            txt.draw(self.W / 2, y0 + 16, self.error, (255, 140, 120), 14, align="center")
        else:
            txt.draw(self.W / 2, y0 + 30, "WASD move · mouse look · LMB shoot · RMB scope · R reload · Space jump · "
                     "Shift sprint", (150, 160, 180), 13, align="center")
            txt.draw(self.W / 2, y0 + 12, "1 rifle · 2 sniper · 3 knife · Q last weapon · wheel switch · H heal · "
                     "TAB scores · Esc menu", (150, 160, 180), 13, align="center")
        return None

    def draw_lobby(self):
        node, ui, txt = self.node, self.ui, self.r.txt
        x0, y0 = self.panel(820, 520)
        top = y0 + 520
        title = "PRACTICE" if self.practice else "LOBBY"
        txt.draw(x0 + 30, top - 50, title, (255, 255, 255), 30)
        txt.draw(x0 + 790, top - 46, node.arena_summary(), (190, 205, 230), 15, align="right")
        cols = [(40, "NODE"), (110, "NAME"), (260, "STATUS"), (370, "LINK"), (480, "PING"), (580, "PKT/S"),
                (680, "RATE / MAX")]
        y = top - 96
        for cx, lab in cols:
            txt.draw(x0 + cx, y, lab, (150, 165, 190), 13)
        rows = [(node.node, node.me.name, "you" + (" (HOST)" if node.role == "host" else ""), "-", "-", "-",
                 f"{node.rate:g} / {node.max_players}")]
        for p in node.roster():
            st = node.peer_status(p)
            ping = f"{p.rtt * 1000:.0f} ms" if p.rtt is not None else "-"
            rows.append((p.pid, p.name, st, node.link_state(p), ping, f"{p.pkt_rate(node.now):.1f}",
                         f"{p.rate:g} / {p.max_players}"))
        heard = {r[0] for r in rows}
        for pid in self.peers_expected:
            if pid not in heard:
                rows.append((pid, "-", "not heard", "-", "-", "-", "-"))
        for row in rows[:10]:
            y -= 30
            rect(x0 + 24, y - 6, x0 + 30, y + 16, color_of(row[0]), 1.0)
            stc = {"HOST": (140, 255, 160), "arena": (170, 220, 255), "ready": (255, 230, 150),
                   "lost": (255, 140, 120), "not heard": (150, 155, 170)}.get(row[2], (230, 235, 245))
            lnc = {"two-way": (150, 255, 170), "one-way": (255, 190, 100), "lost": (255, 140, 120)}.get(row[3], (220, 225, 235))
            vals = [(str(row[0]), (255, 255, 255)), (row[1], (255, 255, 255)), (row[2], stc), (row[3], lnc),
                    (row[4], (220, 225, 235)), (row[5], (220, 225, 235)), (row[6], (200, 205, 215))]
            for (cx, _), (val, c) in zip(cols, vals):
                txt.draw(x0 + cx, y, val, c, 15)
        if len(heard) == 1:
            txt.draw(self.W / 2, y - 40, "No other node heard yet - is the flowgraph running on the other PCs?",
                     (255, 220, 160), 15, align="center")
        # status + buttons
        status = node.role_text()
        txt.draw(self.W / 2, y0 + 92, status if node.mode == "arena" else
                 "Enter arena: join the running arena, or elect a host among the players who want to play",
                 (255, 230, 160) if node.mode == "arena" else (180, 190, 210), 15, align="center")
        if node.mode == "lobby":
            ok, why = node.can_join()
            if ui.button("enter", x0 + 220, y0 + 24, x0 + 480, y0 + 70, "Enter arena" if ok else why, primary=True,
                         enabled=ok, size=20):
                node.join_game()
        else:
            if ui.button("cancel", x0 + 220, y0 + 24, x0 + 480, y0 + 70, "Cancel"):
                node.leave_game()
        if ui.button("menu", x0 + 500, y0 + 24, x0 + 680, y0 + 70, "Main menu"):
            self.to_menu()
        for i, (text, ttl) in enumerate(list(node.messages)[-3:]):
            txt.draw(self.W / 2, y0 + 130 + 22 * i, text, (255, 255, 255), 15, align="center")

    def draw_pause(self):
        ui, txt = self.ui, self.r.txt
        items = [("resume", "Resume"), ("lobby", "Leave arena (lobby)"), ("menu", "Main menu"), ("quit", "Quit game")]
        if self.ctl_port:
            items.append(("closeall", "Close everything"))
        ph = 330 + 56 * (len(items) - 4)
        x0, y0 = self.panel(380, ph)
        txt.draw(self.W / 2, y0 + ph - 50, "PAUSED", (255, 255, 255), 28, align="center")
        txt.draw(self.W / 2, y0 + ph - 76, "the game goes on - you are still in the arena", (170, 180, 200), 13,
                 align="center")
        b = []
        for i, (ident, lab) in enumerate(items):
            yy = y0 + ph - 134 - i * 56
            if ui.button(ident, x0 + 60, yy, x0 + 320, yy + 44, lab, primary=(ident == "resume")):
                b.append(ident)
        return b[0] if b else None


# ====================================================================== main
def main(argv=None):
    env = os.environ.get
    env_arq = env("CDP_GAME_ARQ", "").strip()
    ap = argparse.ArgumentParser(description="Radio Arena v3 - multi-player 3D shooter over the CDP radio (v6.9 or v6.8)")
    ap.add_argument("--node", type=int, default=None, help="this node's ID (default: $CDP_MY_ID)")
    ap.add_argument("--stack", choices=("v69", "v68"), default=None,
                    help="v69 (default, or $CDP_GAME_STACK): cdp_link v6.9 socket bridge; v68: the v6.8 flowgraph's "
                         "Game Link block (chosen automatically when --tx-port / --rx-port are given, as the v6.8 "
                         "flowgraph does)")
    ap.add_argument("--udp-port", type=int, default=int(env("CDP_UDP_PORT", "5200")),
                    help="v6.9: bridge datagram port (game -> radio), default $CDP_UDP_PORT or 5200")
    ap.add_argument("--udp-out-port", type=int, default=int(env("CDP_UDP_OUT_PORT", "5201")),
                    help="v6.9: port the bridge sends received datagrams to, default $CDP_UDP_OUT_PORT or 5201")
    ap.add_argument("--arq-port", type=int, default=int(env("CDP_ARQ_PORT", "5100")),
                    help="v6.9: bridge reliable-message TCP port, default $CDP_ARQ_PORT or 5100")
    ap.add_argument("--tx-port", type=int, default=52001, help="v6.8: UDP port of the Game Link block (game -> radio)")
    ap.add_argument("--rx-port", type=int, default=52002, help="v6.8: UDP port the Game Link block sends to")
    num = lambda k, f: f(env(k)) if (env(k) or "").strip() else None          # noqa: E731
    ap.add_argument("--rate", type=float, default=num("CDP_GAME_RATE", float),
                    help="packets/s (menu default; or $CDP_GAME_RATE)")
    ap.add_argument("--max-players", type=int, default=num("CDP_GAME_MAX", int),
                    help="arena size (menu default; or $CDP_GAME_MAX)")
    ap.add_argument("--max-lag", type=float, default=num("CDP_GAME_LAG", float),
                    help="lag limit in s (menu default; or $CDP_GAME_LAG)")
    ap.add_argument("--arq", dest="arq", action="store_true", default=(env_arq not in ("0", "")) if env_arq else None,
                    help="inputs to the host with the SR-ARQ (default: $CDP_GAME_ARQ, else the menu)")
    ap.add_argument("--no-arq", dest="arq", action="store_false",
                    help="ARQ bypass: v6.9 unicast datagrams, v6.8 everything broadcast")
    ap.add_argument("--name", default=None)
    ap.add_argument("--practice", type=int, default=None, metavar="BOTS", help="start practice vs BOTS bots at once")
    ap.add_argument("--online", action="store_true", help="skip the menu: go to the lobby at once")
    ap.add_argument("--sens", type=float, default=0.12, help="mouse sensitivity (deg per pixel)")
    ap.add_argument("--fov", type=float, default=80.0)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--msaa", type=int, default=4, help="anti-aliasing samples (0 = off)")
    ap.add_argument("--x11", action="store_true", help="use the X11 (XWayland) backend")
    ap.add_argument("--frames", type=int, default=0, help=argparse.SUPPRESS)       # self-test: quit after N frames
    a = ap.parse_args(argv)
    if a.stack is None:
        given = list(sys.argv[1:] if argv is None else argv)
        v68_ports = any(x.split("=")[0] in ("--tx-port", "--rx-port") for x in given)
        a.stack = env("CDP_GAME_STACK", "").strip() or ("v68" if v68_ports else "v69")

    pygame.init()
    W, H = (int(v) for v in a.size.lower().split("x"))
    open_window(W, H, a.fullscreen, a.msaa)
    W, H = pygame.display.get_surface().get_size()
    pygame.display.set_caption("Radio Arena")
    print(f"window open: SDL {pygame.display.get_driver()}, PyOpenGL platform "
          f"{os.environ.get('PYOPENGL_PLATFORM', 'auto')}", flush=True)
    try:
        rend = Renderer(W, H, a.fov)
    except Exception as e:
        drv = pygame.display.get_driver() if pygame.display.get_init() else "?"
        print(f"\nOpenGL setup failed: {e!r}\n  SDL video driver: {drv}, PYOPENGL_PLATFORM="
              f"{os.environ.get('PYOPENGL_PLATFORM', '(unset)')}\n"
              f"  try:  PYOPENGL_PLATFORM=egl python3 game/fps_game.py\n"
              f"   or:  PYOPENGL_PLATFORM=linux python3 game/fps_game.py --x11", flush=True)
        raise
    app = App(a, rend, W, H)
    if a.practice is not None:
        app.s["bots"] = max(1, min(7, a.practice))
        app.start_practice()
    elif a.online:
        app.start_online()

    mouse = MouseLook(W, H)
    grabbed = False
    focus_gained = getattr(pygame, "WINDOWFOCUSGAINED", None)
    clock = pygame.time.Clock()
    recoil = 0.0
    sens = math.radians(a.sens)
    key_turn = math.radians(150)
    running = True
    frame = 0
    log_t = 0.0
    last_weapon = W_SNIPER
    wheel_ev = getattr(pygame, "MOUSEWHEEL", None)
    while running:
        frame += 1
        if a.frames and frame > a.frames:
            break
        dt = min(clock.tick(144) / 1000.0, 0.05)
        node = app.node
        in_game = node is not None and node.mode == "arena" and node.admitted
        want_grab = in_game and not app.paused
        if want_grab != grabbed:
            grabbed = mouse.grab(want_grab)
        look_dx = look_dy = 0.0
        click = None
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if app.key(ev):
                    continue
                if ev.key == pygame.K_F10:
                    running = False
                elif ev.key == pygame.K_ESCAPE:
                    if in_game:
                        app.paused = not app.paused
                    elif app.screen == "lobby":
                        app.to_menu()
                elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    if app.screen == "menu":
                        app.start_online()
                    elif node is not None and node.mode == "lobby":
                        node.join_game()
                elif ev.key == pygame.K_r and grabbed and node is not None:
                    node.start_reload()
                elif ev.key in (pygame.K_1, pygame.K_2, pygame.K_3) and grabbed and node is not None:
                    node.switch_weapon({pygame.K_1: W_RIFLE, pygame.K_2: W_SNIPER, pygame.K_3: W_KNIFE}[ev.key])
                elif ev.key == pygame.K_q and grabbed and node is not None:
                    node.switch_weapon(last_weapon)
                elif ev.key == pygame.K_h and grabbed and node is not None:
                    node.heal()
            elif wheel_ev is not None and ev.type == wheel_ev and grabbed and node is not None and ev.y:
                node.cycle_weapon(-1 if ev.y > 0 else 1)
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                if not grabbed:
                    click = (ev.pos[0], H - ev.pos[1])
                elif mouse.rearm:
                    mouse.grab(True)
            elif focus_gained is not None and ev.type == focus_gained and grabbed:
                mouse.grab(True)
                mouse.rearm = True
            elif ev.type == pygame.MOUSEMOTION and grabbed and node is not None:
                dx, dy = mouse.motion(ev)
                if dx or dy:
                    zoom = rend.fov / rend.base_fov                   # slower turning through the scope
                    node.me.look(dx * sens * zoom, -dy * sens * zoom)
                    look_dx += dx
                    look_dy += dy
        if app.editing and click is not None:
            app.editing = False
        mx, my = pygame.mouse.get_pos()
        app.ui.begin((mx, H - my), click)

        keys = pygame.key.get_pressed()
        fwd = strafe = 0.0
        sprint = jump = firing = False
        if grabbed:
            fwd = (1.0 if keys[pygame.K_w] else 0.0) - (1.0 if keys[pygame.K_s] else 0.0)
            strafe = (1.0 if keys[pygame.K_d] else 0.0) - (1.0 if keys[pygame.K_a] else 0.0)
            sprint = keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]
            jump = keys[pygame.K_SPACE]
            buttons = pygame.mouse.get_pressed()
            firing = buttons[0]
            if node is not None:
                node.set_scope(len(buttons) > 2 and buttons[2])
            turn = (1.0 if keys[pygame.K_RIGHT] else 0.0) - (1.0 if keys[pygame.K_LEFT] else 0.0)
            tilt = (1.0 if keys[pygame.K_UP] else 0.0) - (1.0 if keys[pygame.K_DOWN] else 0.0)
            if (turn or tilt) and node is not None:
                node.me.look(turn * key_turn * dt, tilt * key_turn * 0.6 * dt)
        mouse.end_frame()

        # network + game
        if node is not None:
            shots_before = node.stats["shots"]
            weapon_before = node.me.weapon
            app.step_net(dt, fwd, strafe, sprint, jump, firing)
            if node.stats["shots"] > shots_before and node.me.weapon != W_KNIFE:
                recoil = 1.0
            if node.me.weapon != weapon_before:
                last_weapon = weapon_before
        recoil = max(0.0, recoil - dt * 9)
        log_t += dt
        if log_t >= 5.0:                              # one status line every 5 s -> game.log of the session
            log_t = 0.0
            if node is not None and app.practice is None:
                lk = node.link
                rel = dict(getattr(lk, "rel", {}))
                print(f"[{time.strftime('%H:%M:%S')}] mode {node.mode} role {node.role} host {node.host_id} "
                      f"rate {node.current_rate():.1f}/{node.rate:g} queueing {node.cc_queue * 1000:.0f} ms "
                      f"rtt_host {'-' if node.rtt_host is None else f'{node.rtt_host * 1000:.0f} ms'} "
                      f"peers {sorted(node.peers)} stats {dict(node.stats)} link_ignored {lk.ignored} "
                      f"send_errors {lk.send_errors}" + (f" reliable {rel}" if rel else ""), flush=True)

        # draw
        node = app.node
        if node is not None and node.mode == "arena" and node.admitted:
            rend.draw_game(node, dt, recoil, look_dx, look_dy)
            rend.draw_hud(node, grabbed, keys[pygame.K_TAB] and not app.paused)
            if app.paused:
                rend.begin_2d()
                rect(0, 0, W, H, (0, 0, 0), 0.45)
                act = app.draw_pause()
                rend.end_2d()
                if act == "resume":
                    app.paused = False
                elif act == "lobby":
                    node.leave_game()
                    app.paused = False
                elif act == "menu":
                    app.to_menu()
                elif act == "quit":
                    running = False
                elif act == "closeall":
                    if app.close_everything():
                        running = False
                    else:
                        app.paused = True
        else:
            rend.draw_backdrop(dt)
            rend.begin_2d()
            if app.screen == "menu" or node is None:
                app.screen = "menu"
                if app.draw_menu() == "quit":
                    running = False
            else:
                app.draw_lobby()
            rend.end_2d()
        pygame.display.flip()

    if app.node is not None:
        app.to_menu()
    app.s.save()
    pygame.quit()


if __name__ == "__main__":
    sys.exit(main())

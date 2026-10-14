"""
fps_game.py - simple 2-player 3D arena shooter whose network traffic is the
31-byte payload from payload.py, so it can ride your GNU Radio link.

Controls: WASD move | mouse look, full 360 (arrow keys also turn) | Left mouse shoot (hold = auto)
          R reload | Space jump | Shift sprint | Esc free the mouse (click to recapture) | F10 quit

Run:
    python3 fps_game.py --peer 5              # over the CDP v6.6 flowgraph (UDP block), opponent = node 5,
                                              # this node = $CDP_MY_ID (or --node N)
    python3 fps_game.py --bot                 # offline vs a bot, no radio needed
    python3 fps_game.py --id 0                # plain UDP mode: sends :52001, listens :52002 (own flowgraph)
Usually started from the launcher:  python3 cdp_sock.py launch
See README.md for the GNU Radio hookup.

Needs: pygame (or pygame-ce) and PyOpenGL. No GLU needed.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys

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

from game_core import (BODY_TOP, CELL, COLS, EYE_H, MAG, MAP, MAX_HP, RELOAD_TIME, ROWS,
                       WALL_H, BotLink, CdpLink, Match, UdpLink, look_dir, raycast, solid_at, solid_cell)
from payload import FLAG_RELOADING

# ------------------------------------------------------------------ look & feel
SUN_DIR = (-0.42, 0.62, -0.66)                     # direction *towards* the sun
_l = math.sqrt(sum(c * c for c in SUN_DIR))
SUN_DIR = tuple(c / _l for c in SUN_DIR)
SUN_COL = (1.00, 0.93, 0.80)
AMB_COL = (0.46, 0.52, 0.64)                       # sky light, a bit blue
SKY_ZENITH = (0.22, 0.42, 0.78)
SKY_HORIZON = (0.72, 0.80, 0.90)
FOG_START, FOG_END = 22.0, 75.0
TEAM = {0: (0.18, 0.42, 0.92), 1: (0.90, 0.24, 0.20)}
TEAM_NAME = {0: "BLUE", 1: "RED"}
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


# ------------------------------------------------------------------ baked lighting
def in_sun_shadow(x, y, z):
    """March from a point towards the sun; True if a wall is in the way."""
    lx, ly, lz = SUN_DIR
    t = 0.05
    while True:
        py = y + ly * t
        if py >= WALL_H:
            return False
        if solid_at(x + lx * t, z + lz * t):
            return True
        t += 0.08


def wall_distance(x, z):
    """Distance from a floor point to the nearest wall block (checks the 5x5 cells around)."""
    c0, r0 = int(x // CELL), int(z // CELL)
    best = 9.0
    for r in range(r0 - 2, r0 + 3):
        for c in range(c0 - 2, c0 + 3):
            if solid_cell(r, c):
                dx = max(c * CELL - x, 0.0, x - (c + 1) * CELL)
                dz = max(r * CELL - z, 0.0, z - (r + 1) * CELL)
                best = min(best, math.hypot(dx, dz))
    return best


def light(nx, ny, nz, lit, ao=1.0):
    """Vertex colour multiplier: sky ambient (with AO) + sun (if not shadowed)."""
    ndl = max(0.0, nx * SUN_DIR[0] + ny * SUN_DIR[1] + nz * SUN_DIR[2]) if lit else 0.0
    return tuple(min(1.6, AMB_COL[i] * ao + SUN_COL[i] * ndl * 0.85) for i in range(3))


def build_world_list(tex_wall, tex_floor):
    lst = glGenLists(1)
    glNewList(lst, GL_COMPILE)
    glEnable(GL_TEXTURE_2D)

    # ---- floor: each open cell split into SUB x SUB quads, per-vertex sun shadow + AO
    SUB = 6
    s = CELL / SUB
    cache = {}

    def floor_col(vx, vz):
        key = (round(vx, 3), round(vz, 3))
        if key not in cache:
            ao = 1.0 - 0.45 * math.exp(-wall_distance(vx, vz) / 0.35)
            cache[key] = light(0, 1, 0, not in_sun_shadow(vx, 0.0, vz), ao)
        return cache[key]

    glBindTexture(GL_TEXTURE_2D, tex_floor)
    glBegin(GL_QUADS)
    for r in range(ROWS):
        for c in range(COLS):
            if MAP[r][c] == "#":
                continue
            for i in range(SUB):
                for j in range(SUB):
                    x0, z0 = c * CELL + j * s, r * CELL + i * s
                    for vx, vz in ((x0, z0), (x0, z0 + s), (x0 + s, z0 + s), (x0 + s, z0)):
                        glColor3f(*floor_col(vx, vz))
                        glTexCoord2f(vx / CELL, vz / CELL)
                        glVertex3f(vx, 0.0, vz)
    glEnd()

    # ---- walls: exposed faces split into a grid, per-vertex sun shadow + floor AO
    glBindTexture(GL_TEXTURE_2D, tex_wall)
    HS = [0.0, 0.25, 0.6, 1.2, 1.8, 2.4, WALL_H]
    WS = 4
    wcache = {}
    glBegin(GL_QUADS)
    for r in range(ROWS):
        for c in range(COLS):
            if MAP[r][c] != "#":
                continue
            x0, z0, x1, z1 = c * CELL, r * CELL, (c + 1) * CELL, (r + 1) * CELL
            faces = []
            # u runs so that (u, y) quads wind counter-clockwise seen from outside
            if not solid_cell(r - 1, c):
                faces.append(((0, 0, -1), lambda u: (x1 - u, z0)))
            if not solid_cell(r + 1, c):
                faces.append(((0, 0, 1), lambda u: (x0 + u, z1)))
            if not solid_cell(r, c - 1):
                faces.append(((-1, 0, 0), lambda u: (x0, z0 + u)))
            if not solid_cell(r, c + 1):
                faces.append(((1, 0, 0), lambda u: (x1, z1 - u)))
            for n, pos in faces:
                for k in range(WS):
                    u0, u1 = CELL * k / WS, CELL * (k + 1) / WS
                    for h in range(len(HS) - 1):
                        quad = ((u0, HS[h]), (u1, HS[h]), (u1, HS[h + 1]), (u0, HS[h + 1]))
                        for u, y in quad:
                            wx, wz = pos(u)
                            key = (round(wx, 3), round(y, 3), round(wz, 3), n)
                            if key not in wcache:
                                ao = 0.62 + 0.38 * min(1.0, y / 0.9) ** 0.7
                                lit = not in_sun_shadow(wx + n[0] * 0.05, y, wz + n[2] * 0.05)
                                wcache[key] = light(n[0], n[1], n[2], lit, ao)
                            glColor3f(*wcache[key])
                            tu = (wx + wz) / CELL
                            glTexCoord2f(tu, y / CELL)
                            glVertex3f(wx, y, wz)
            # wall top
            glColor3f(*(v * 0.75 for v in light(0, 1, 0, True)))
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
class Renderer:
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
        cdata, csize = make_cloud_texture(rng)
        self.tex_cloud = upload_texture(cdata, csize, GL_RGBA)
        self.world = build_world_list(self.tex_wall, self.tex_floor)

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
    def draw_blob_shadow(self, x, z, r=0.45):
        glDisable(GL_CULL_FACE)
        glDepthMask(GL_FALSE)
        glBegin(GL_TRIANGLE_FAN)
        glColor4f(0, 0, 0, 0.45)
        glVertex3f(x, 0.015, z)
        glColor4f(0, 0, 0, 0.0)
        for i in range(21):
            a = 2 * math.pi * i / 20
            glVertex3f(x + math.cos(a) * r, 0.015, z + math.sin(a) * r)
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
        f, r, u = 0.85, 0.13, -0.13
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

        # reload: gun tilts and dips, left hand pulls the magazine and brings it back
        rl, p = 0.0, 0.0
        if me.reloading:
            p = 1.0 - me.reload_t / RELOAD_TIME
            rl = math.sin(math.pi * min(1.0, p))
        bx = math.sin(self.bob_phase) * 0.012 * self.bob_amp
        by = -abs(math.cos(self.bob_phase)) * 0.010 * self.bob_amp
        glTranslatef(0.165 + bx + self.sway[0], -0.180 + by - rl * 0.05 + self.sway[1], -0.43 + recoil * 0.035)
        glRotatef(recoil * 4.0 + rl * 14, 1, 0, 0)
        glRotatef(rl * -28, 0, 0, 1)
        glRotatef(4.5, 0, 1, 0)                                   # toe the muzzle in towards the centre
        glScalef(0.52, 0.52, 0.52)

        metal = (0.30, 0.31, 0.33)
        metal_d = (0.17, 0.17, 0.19)
        poly = (0.33, 0.30, 0.24)
        glove = (0.15, 0.14, 0.12)
        sleeve = mul(team, 0.80)
        # rifle (z is forward = negative)
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
        out = 0.0
        if me.reloading:                                                # mag out in the middle of the reload
            out = 0.22 * max(0.0, min(1.0, (min(p, 1 - p) - 0.15) * 4.0))
        box(-0.024, -0.215 - out, -0.26, 0.024, -0.055 - out, -0.165, metal_d)  # magazine

        # hands and forearms; elbows sit below/behind the screen edge so arms run off-screen
        box(-0.036, -0.155, -0.070, 0.040, -0.075, 0.012, glove)        # right hand on the grip
        beam((0.0, -0.13, -0.01), (0.30, -0.62, 0.42), 0.075, 0.080, sleeve)
        beam((0.0, -0.13, -0.01), (0.05, -0.20, 0.07), 0.085, 0.090, glove)   # wrist/cuff
        if me.reloading:
            hand = (-0.02, -0.20 - out * 0.9, -0.22)                      # left hand on the magazine
        else:
            hand = (-0.01, -0.075, -0.47)                                 # left hand under the handguard
        hx, hy, hz = hand
        box(hx - 0.040, hy - 0.035, hz - 0.055, hx + 0.040, hy + 0.030, hz + 0.055, glove)
        beam((hx, hy - 0.02, hz + 0.02), (-0.46, -0.58, 0.05), 0.075, 0.080, sleeve)

        glDisable(GL_LIGHTING)
        glDisable(GL_COLOR_MATERIAL)
        if recoil > 0.55:
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

    # ---------------------------------------------------------- HUD
    def draw_hud(self, m, link_desc, grabbed):
        W, H, txt = self.W, self.H, self.txt
        self.ortho()
        glDisable(GL_DEPTH_TEST)
        glDisable(GL_FOG)
        glDisable(GL_CULL_FACE)
        me, rv, now = m.me, m.remote, m.now
        cx, cy = W / 2, H / 2

        # vignette (+ red when hurt)
        hurt = min(1.0, m.damage_t / 0.4) if m.damage_t > 0 else 0.0
        low = 0.35 if me.alive and me.hp <= 30 else 0.0
        red = max(hurt, low * (0.6 + 0.4 * math.sin(now * 5)))
        edge = (0.5 * red + 0.0, 0.0, 0.0)
        ea = 0.45 + 0.35 * red
        inner = 0.30
        glBegin(GL_QUADS)
        for (ax, ay), (bx, by) in (((0, 0), (W, 0)), ((W, 0), (W, H)), ((W, H), (0, H)), ((0, H), (0, 0))):
            ix0, iy0 = ax + (cx - ax) * inner, ay + (cy - ay) * inner
            ix1, iy1 = bx + (cx - bx) * inner, by + (cy - by) * inner
            glColor4f(*edge, ea)
            glVertex2f(ax, ay); glVertex2f(bx, by)
            glColor4f(*edge, 0.0)
            glVertex2f(ix1, iy1); glVertex2f(ix0, iy0)
        glEnd()

        # crosshair (spreads while moving / firing)
        if me.alive:
            gap = 6 + 10 * min(1.0, self.bob_amp) + 9 * self.recoil_vis
            ln = 10
            glLineWidth(2)
            glBegin(GL_LINES)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                glColor4f(0, 0, 0, 0.6)
                glVertex2f(cx + dx * gap + 1, cy + dy * gap - 1)
                glVertex2f(cx + dx * (gap + ln) + 1, cy + dy * (gap + ln) - 1)
                glColor4f(1, 1, 1, 0.95)
                glVertex2f(cx + dx * gap, cy + dy * gap)
                glVertex2f(cx + dx * (gap + ln), cy + dy * (gap + ln))
            glEnd()
            rect(cx - 1, cy - 1, cx + 1, cy + 1, (1, 1, 1), 0.9)
            if m.hitmarker_t > 0:
                big = m.hitmarker_t > 0.16
                c = (1.0, 0.25, 0.2) if big else (1.0, 1.0, 1.0)
                glLineWidth(3 if big else 2)
                glColor4f(*c, 1)
                glBegin(GL_LINES)
                for dx, dy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
                    glVertex2f(cx + dx * 8, cy + dy * 8)
                    glVertex2f(cx + dx * 17, cy + dy * 17)
                glEnd()

        # health panel
        rect(18, 18, 300, 74, (0.02, 0.03, 0.05), 0.55)
        rect(18, 72, 300, 74, TEAM[me.pid], 0.9)
        frac = me.hp / MAX_HP
        hc = (0.25 + 0.7 * (1 - frac), 0.80 * frac + 0.12, 0.25)
        rect(92, 30, 288, 46, (0.0, 0.0, 0.0), 0.5)
        rect(92, 30, 92 + 196 * frac, 46, hc, 0.95)
        rect(92, 41, 92 + 196 * frac, 46, (1, 1, 1), 0.15)
        rect(36, 37, 64, 43, (1, 1, 1), 0.95)                            # + icon
        rect(47, 26, 53, 54, (1, 1, 1), 0.95)
        txt.draw(92, 48, f"{me.hp}", (255, 255, 255), 18)
        txt.draw(288, 48, "HEALTH", (190, 200, 215), 12, align="right")

        # ammo panel
        rect(W - 300, 18, W - 18, 74, (0.02, 0.03, 0.05), 0.55)
        rect(W - 300, 72, W - 18, 74, TEAM[me.pid], 0.9)
        if me.reloading:
            p = 1.0 - me.reload_t / RELOAD_TIME
            rect(W - 288, 26, W - 30, 34, (0.0, 0.0, 0.0), 0.5)
            rect(W - 288, 26, W - 288 + 258 * p, 34, (1.0, 0.8, 0.3), 0.95)
            txt.draw(W - 30, 40, "RELOADING", (255, 210, 90), 22, align="right")
        else:
            col = (255, 120, 90) if me.ammo <= 5 else (255, 255, 255)
            w = txt.draw(W - 30, 42, f"/ {MAG}", (170, 180, 195), 16, align="right")
            txt.draw(W - 36 - w, 38, f"{me.ammo}", col, 26, align="right")
            txt.draw(W - 288, 44, "AMMO", (190, 200, 215), 12)
            for i in range(MAG):                                         # bullet pips
                x = W - 288 + i * 8.6
                full = i < me.ammo
                rect(x, 26, x + 6, 36 if full else 28,
                     (1.0, 0.82, 0.35) if full else (0.45, 0.45, 0.45), 0.95 if full else 0.6)
        if me.alive and not me.reloading and me.ammo <= 5:
            txt.draw(cx, cy - 70, "R  to reload", (255, 210, 90), 18, align="center")

        # score (top centre)
        kills, deaths = m.score()
        opp = 1 - me.pid
        rect(cx - 130, H - 52, cx + 130, H - 12, (0.02, 0.03, 0.05), 0.55)
        rect(cx - 130, H - 52, cx - 126, H - 12, TEAM[me.pid], 1.0)
        rect(cx + 126, H - 52, cx + 130, H - 12, TEAM[opp], 1.0)
        txt.draw(cx - 118, H - 43, f"YOU P{me.pid}", (200, 215, 255), 14)
        txt.draw(cx + 118, H - 43, f"P{opp}", (255, 205, 200), 14, align="right")
        txt.draw(cx, H - 47, f"{kills}  :  {deaths}", (255, 255, 255), 26, align="center")

        # link status (top left)
        if rv.connected(now):
            s = rv.state
            link = f"P{s.pid} linked   {rv.pkt_rate(now):4.1f} pkt/s   loss {rv.loss_pct():4.1f}%   bad {rv.bad}"
            rect(14, H - 36, 18, H - 14, (0.3, 1.0, 0.4), 1.0)
            txt.draw(26, H - 34, link, (170, 255, 180), 14)
            extra = "reloading" if s.flags & FLAG_RELOADING else ""
            txt.draw(26, H - 54, f"Opponent HP {s.hp if rv.alive else 0}  {extra}", (225, 230, 240), 14)
        else:
            what = "link lost" if rv.state else "waiting for opponent"
            rect(14, H - 36, 18, H - 14, (1.0, 0.6, 0.3), 1.0)
            txt.draw(26, H - 34, f"{what}   [{link_desc}]", (255, 190, 140), 14)

        self.draw_minimap(m)

        # messages
        y = cy + 130
        for text, ttl in m.messages:
            txt.draw(cx, y, text, (255, 255, 255), 24, align="center")
            y -= 34

        if not me.alive:
            rect(0, 0, W, H, (0.15, 0, 0), 0.45)
            txt.draw(cx, cy + 10, "ELIMINATED", (255, 90, 90), 44, align="center")
            txt.draw(cx, cy - 30, f"respawn in {max(0, me.dead_t):.1f}s", (240, 240, 240), 20, align="center")

        if not grabbed:
            rect(0, 0, W, H, (0, 0, 0), 0.35)
            txt.draw(cx, cy + 50, "Click to play", (255, 255, 200), 30, align="center")
            txt.draw(cx, cy + 18, "Esc frees the mouse   F10 quits", (220, 220, 220), 16, align="center")
            txt.draw(cx, cy - 8, "Arrow keys also turn.  If turning stops at the screen edge, start with --x11",
                     (200, 200, 200), 14, align="center")

        glEnable(GL_DEPTH_TEST)
        glEnable(GL_FOG)
        glEnable(GL_CULL_FACE)

    def draw_minimap(self, m):
        W, H = self.W, self.H
        sc = max(5.0, min(9.0, H / 80.0))           # pixels per map cell
        size = COLS * sc
        x0, y0 = W - size - 18, H - size - 18
        rect(x0 - 6, y0 - 6, x0 + size + 6, y0 + size + 6, (0.02, 0.03, 0.05), 0.55)
        glBegin(GL_QUADS)
        for r in range(ROWS):
            for c in range(COLS):
                if MAP[r][c] == "#":
                    glColor4f(0.80, 0.74, 0.62, 0.85)
                    X, Y = x0 + c * sc, y0 + size - (r + 1) * sc
                    glVertex2f(X, Y); glVertex2f(X + sc, Y); glVertex2f(X + sc, Y + sc); glVertex2f(X, Y + sc)
        glEnd()

        def to_px(x, z):
            return x0 + x / CELL * sc, y0 + size - z / CELL * sc

        def arrow(x, z, yaw, col):
            px, py = to_px(x, z)
            dx, dy = math.sin(yaw), math.cos(yaw)
            glColor4f(*col, 1)
            glBegin(GL_TRIANGLES)
            glVertex2f(px + dx * 8, py + dy * 8)
            glVertex2f(px - dx * 4 + dy * 5, py - dy * 4 - dx * 5)
            glVertex2f(px - dx * 4 - dy * 5, py - dy * 4 + dx * 5)
            glEnd()

        me, rv = m.me, m.remote
        if rv.connected(m.now) and rv.alive:
            arrow(rv.x, rv.z, rv.yaw, TEAM.get(rv.state.pid, (1, 1, 0)))
        if me.alive:
            arrow(me.x, me.z, me.yaw, (1.0, 1.0, 1.0))

    # ---------------------------------------------------------- frame
    def draw(self, m, dt, recoil, grabbed, link_desc, look_dx=0.0, look_dy=0.0):
        self.clock += dt
        me, rv = m.me, m.remote
        self.cam_yaw, self.cam_pitch = me.yaw, me.pitch
        self.recoil_vis = recoil

        # movement-derived animation state
        if self.last_me is not None and me.alive:
            sp = math.hypot(me.x - self.last_me[0], me.z - self.last_me[1]) / max(dt, 1e-4)
        else:
            sp = 0.0
        self.last_me = (me.x, me.z)
        on_ground = me.y <= 0.01
        target_amp = min(1.4, sp / 5.0) if on_ground else 0.0
        self.bob_amp += (target_amp - self.bob_amp) * min(1.0, dt * 10)
        self.bob_phase += dt * (6.0 + sp * 1.1) * (1 if sp > 0.2 else 0)
        self.sway[0] += (-look_dx * 0.0006 - self.sway[0]) * min(1.0, dt * 9)
        self.sway[1] += (look_dy * 0.0006 - self.sway[1]) * min(1.0, dt * 9)
        self.sway = [max(-0.04, min(0.04, v)) for v in self.sway]

        if self.last_rv is not None:
            rsp = math.hypot(rv.x - self.last_rv[0], rv.z - self.last_rv[1]) / max(dt, 1e-4)
        else:
            rsp = 0.0
        self.last_rv = (rv.x, rv.z)
        self.rv_speed += (rsp - self.rv_speed) * min(1.0, dt * 8)
        self.walk_phase += dt * (2.0 + self.rv_speed * 2.0) if self.rv_speed > 0.3 else 0.0

        # impacts from my shots and the opponent's
        while m.shots:
            self.spawn_impact(*m.shots.popleft())
        if rv.connected(m.now) and rv.alive and rv.tracer_t > self.prev_tracer:
            d = look_dir(rv.yaw, rv.pitch)
            ex, ey, ez = rv.x, rv.y + EYE_H, rv.z
            kind, dist, _ = raycast(ex, ey, ez, d, max_dist=60, step=0.05)
            self.remote_shot_dist = dist
            if kind in ("wall", "floor"):
                self.spawn_impact((ex + d[0] * dist, ey + d[1] * dist, ez + d[2] * dist), d, kind)
        self.prev_tracer = rv.tracer_t
        if not hasattr(self, "remote_shot_dist"):
            self.remote_shot_dist = 20.0
        self.update_effects(dt)

        # ---- 3D scene
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        self.perspective(self.fov, 0.05, 200.0)
        glLoadIdentity()
        shake = m.damage_t * 2.5
        glRotatef(-math.degrees(me.pitch) - recoil * 0.6 + self.rng.uniform(-shake, shake), 1, 0, 0)
        glRotatef(math.degrees(me.yaw) + self.rng.uniform(-shake, shake), 0, 1, 0)
        self.draw_sky()
        glTranslatef(-me.x, -(me.y + EYE_H), -me.z)
        self.draw_clouds(me.x, me.z)
        glEnable(GL_FOG)
        glCallList(self.world)

        if rv.connected(m.now):
            self.draw_player(rv, rv.state.pid, not rv.alive, m.hitmarker_t, rv.tracer_t > 0, dt)
        self.draw_effects()
        self.draw_tracers(m)

        if me.alive:
            self.draw_viewmodel(me, recoil, TEAM.get(me.pid, (0.5, 0.5, 0.5)))

        self.draw_hud(m, link_desc, grabbed)


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


def main(argv=None):
    ap = argparse.ArgumentParser(description="2-player 3D shooter over a GNU Radio link")
    ap.add_argument("--peer", type=int, default=None,
                    help="play over the CDP v6.6 flowgraph against this node ID (its CDP_MY_ID)")
    ap.add_argument("--node", type=int, default=None, help="this node's ID for --peer (default: $CDP_MY_ID)")
    ap.add_argument("--id", type=int, default=0, choices=(0, 1), help="plain UDP mode: your player id 0/1")
    ap.add_argument("--tx-host", default="127.0.0.1")
    ap.add_argument("--tx-port", type=int, default=52001, help="UDP port the TX flowgraph listens on")
    ap.add_argument("--rx-port", type=int, default=52002, help="UDP port the RX flowgraph sends to")
    ap.add_argument("--rate", type=float, default=None,
                    help="packets per second to send (default 5 over the CDP radio, 10 otherwise)")
    ap.add_argument("--bot", action="store_true", help="play offline against a bot (no radio)")
    ap.add_argument("--loss", type=float, default=0.0, help="bot mode: simulated packet loss 0..1")
    ap.add_argument("--sens", type=float, default=0.12, help="mouse sensitivity (deg per pixel)")
    ap.add_argument("--fov", type=float, default=80.0)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--msaa", type=int, default=4, help="anti-aliasing samples (0 = off)")
    ap.add_argument("--x11", action="store_true",
                    help="use the X11 (XWayland) backend - try this if mouse turning still stops on Wayland")
    ap.add_argument("--frames", type=int, default=0, help=argparse.SUPPRESS)   # self-test: quit after N frames
    a = ap.parse_args(argv)

    if a.peer is not None and not a.bot:
        node = a.node if a.node is not None else os.environ.get("CDP_MY_ID", "").strip()
        if node in ("", None) or not str(node).isdigit():
            sys.exit("--peer needs this node's ID: export CDP_MY_ID=<id> (same as the flowgraph) or use --node")
        node = int(node)
        rate = a.rate or 5.0
        try:
            link = CdpLink(node, a.peer)
        except (OSError, ValueError) as e:
            sys.exit(f"cannot start the radio link: {e}")
        pid = CdpLink.game_pid(node, a.peer)
        print(f"{link.desc}: you are P{pid} ({'blue' if pid == 0 else 'red'}), sending {rate:g} packets/s", flush=True)
    elif a.bot:
        rate = a.rate or 10.0
        pid = a.id
        link = BotLink(1 - pid, loss=a.loss, rate=rate)
    else:
        rate = a.rate or 10.0
        pid = a.id
        link = UdpLink(a.tx_host, a.tx_port, a.rx_port)
    match = Match(pid, link, send_rate=rate)

    pygame.init()
    W, H = (int(v) for v in a.size.lower().split("x"))
    open_window(W, H, a.fullscreen, a.msaa)
    W, H = pygame.display.get_surface().get_size()
    pygame.display.set_caption(f"Radio Arena - P{pid} - {link.desc}")
    print(f"window open: SDL {pygame.display.get_driver()}, PyOpenGL platform "
          f"{os.environ.get('PYOPENGL_PLATFORM', 'auto')}", flush=True)
    try:
        rend = Renderer(W, H, a.fov)
    except Exception as e:
        drv = pygame.display.get_driver() if pygame.display.get_init() else "?"
        print(f"\nOpenGL setup failed: {e!r}\n  SDL video driver: {drv}, PYOPENGL_PLATFORM="
              f"{os.environ.get('PYOPENGL_PLATFORM', '(unset)')}, XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '')}\n"
              f"  try:  PYOPENGL_PLATFORM=egl python3 game/fps_game.py --bot\n"
              f"   or:  PYOPENGL_PLATFORM=linux python3 game/fps_game.py --bot --x11", flush=True)
        raise

    mouse = MouseLook(W, H)
    grabbed = mouse.grab(True)
    focus_gained = getattr(pygame, "WINDOWFOCUSGAINED", None)
    clock = pygame.time.Clock()
    recoil = 0.0
    sens = math.radians(a.sens)
    key_turn = math.radians(150)        # arrow keys: deg/s
    running = True

    frame = 0
    while running:
        frame += 1
        if a.frames and frame > a.frames:
            break
        dt = min(clock.tick(144) / 1000.0, 0.05)
        look_dx = look_dy = 0.0
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    grabbed = mouse.grab(False)
                elif ev.key == pygame.K_F10:
                    running = False
                elif ev.key == pygame.K_r and grabbed:
                    match.me.start_reload()
            elif ev.type == pygame.MOUSEBUTTONDOWN and (not grabbed or mouse.rearm):
                grabbed = mouse.grab(True)
            elif focus_gained is not None and ev.type == focus_gained and grabbed:
                mouse.grab(True)                         # re-arm relative mode after alt-tab
                mouse.rearm = True                       # ... and again on the first click
            elif ev.type == pygame.MOUSEMOTION:
                dx, dy = mouse.motion(ev)
                if dx or dy:
                    match.me.look(dx * sens, -dy * sens)
                    look_dx += dx
                    look_dy += dy

        keys = pygame.key.get_pressed()
        fwd = strafe = 0.0
        sprint = jump = firing = False
        if grabbed:
            fwd = (1.0 if keys[pygame.K_w] else 0.0) - (1.0 if keys[pygame.K_s] else 0.0)
            strafe = (1.0 if keys[pygame.K_d] else 0.0) - (1.0 if keys[pygame.K_a] else 0.0)
            sprint = keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]
            jump = keys[pygame.K_SPACE]
            firing = pygame.mouse.get_pressed()[0]
            turn = (1.0 if keys[pygame.K_RIGHT] else 0.0) - (1.0 if keys[pygame.K_LEFT] else 0.0)
            tilt = (1.0 if keys[pygame.K_UP] else 0.0) - (1.0 if keys[pygame.K_DOWN] else 0.0)
            if turn or tilt:
                match.me.look(turn * key_turn * dt, tilt * key_turn * 0.6 * dt)
        mouse.end_frame()

        ammo_before = match.me.ammo
        match.step(dt, fwd, strafe, sprint, jump, firing)
        if match.me.ammo < ammo_before:
            recoil = 1.0
        recoil = max(0.0, recoil - dt * 9)

        rend.draw(match, dt, recoil, grabbed, link.desc, look_dx, look_dy)
        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    sys.exit(main())

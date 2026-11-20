# WatchDogsGo — OPCJONALNA animacja boot-logo (splash intro)

Splash już działa bez tego (rysowane logo + animowany goryl-operator). To jest
**opcjonalny upgrade** — krótka animacja odtwarzana na środku splasha, nad listą
system-checku. Jak wygenerujesz, podepnę ją pod `wdgong_ui/assets/boot/`.

Format (najlepszy → gorszy): **animowany WebP / APNG** (pełna alfa) > **GIF** (alfa 1-bit).
Tło **przezroczyste (alfa)**. Krótka, bezszwowa pętla ~1.5–2 s, ~12–20 klatek, poziomy.
Nazwij plik `boot` + rozszerzenie (np. `boot.gif` / `boot.webp`).

## WSPÓLNY BLOK (styl) — wklej przed promptem
```
    Cyberpunk "ctOS" boot-up animation for a hacking tool called WatchDogsGo.
    Neon palette ONLY: cyan #22D3DB, magenta #FF2E88, electric violet #B23BD6, ice-blue
    #82DCFF, white highlights; background fully TRANSPARENT (real alpha), no scene, no frame,
    no text unless specified. Crisp vector-ish glow, subtle scanlines/glitch, dark-tech,
    Watch Dogs hacker vibe. Seamless loop, transparent in every frame. Horizontal, high-res.
```

## 1) Wariant A — rozpad/skład logo-znaku (preferowany)
```
A glowing emblem materializes from scattered digital particles and PCB-trace lines that
snake inward and assemble into a single sharp mark: a stylized skull fused with a Wi-Fi /
signal glyph (the WatchDogsGo marker). On assembly it pulses once with a cyan shockwave
ring, a quick magenta glitch flickers across it, then it holds. Loop: disperse -> reassemble
-> pulse. Centered, transparent background.
```

## 2) Wariant B — skanujący pierścień / radar HUD
```
A circular ctOS HUD boot ring: concentric cyan rings draw themselves clockwise, tick marks
and tiny data glyphs populate around the rim, a radar sweep passes once leaving a fading
magenta trail, and a small skull-signal glyph locks into the center with a bright flash.
Loop: rings draw -> sweep -> lock -> dim. Centered, transparent background.
```

## 3) Wariant C — „łączenie z siecią" (neural links)
```
A central glowing node with thin PCB-trace tendrils shooting outward to small cluster
nodes, data pulses (micro dots with streaks) travel inward along the traces and charge the
center until it flares with a cyan+magenta burst. Matches the in-game neural-link look.
Loop: traces light up -> pulses converge -> flare -> reset. Centered, transparent background.
```

Po Twojej stronie: wrzuć plik do tego folderu jako `boot.<ext>`. Ja rozbiję na klatki
(ImageMagick `-coalesce`, pełna alfa) i podepnę do splasha między logo a listą checków.

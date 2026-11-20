# WatchDogsGo — animowane postacie (goryl WDGWars) per zakładka menu

7 postaci. Przy zmianie zakładki w menu (SCAN / SNIFF / ATTACK / ADDONS / SYSTEM /
EXTRAS / CRISIS) pokazuje się inna. **Każda to ten sam cyberpunkowy GORYL — maskotka
WDGWars** — robiący krótką, zapętloną akcję pasującą do zakładki. **Tło przezroczyste
(alfa) + CAŁA postać head-to-toe (nie od pasa!).**

Pliki wrzuć do podfolderu o dokładnej nazwie zakładki (małe litery):
`characters/scan/`, `sniff/`, `attack/`, `addons/`, `system/`, `extras/`, `crisis/`
— klatki PNG w środku (np. `00.png, 01.png, …`). Silnik ładuje maskotkę PO NAZWIE
zakładki, więc EXTRAS/CRISIS dostają własne klipy automatycznie.

> **Uwaga do GPT image-gen:** modele uparcie robią POPIERSIE i wstawiają TŁO. W nowych
> promptach (EXTRAS/CRISIS/SYSTEM poniżej) wymogi „full body head-to-toe" i „100%
> transparent, no background" są WPISANE NA POCZĄTKU, powtórzone, + lista „AVOID". Jak
> mimo to przycina: dopisz na końcu „full body in frame, tiny character, lots of empty
> space around it". Brak alfy → poproś o czyste białe tło, usunę je po stronie silnika.

Format (najlepszy → gorszy dla przezroczystości):
1. **animowany WebP** lub **APNG** — pełna alfa (miękkie krawędzie, glow) ← preferowane
2. **GIF** — działa, ale alfa 1-bit (twarda krawędź, może być „schodek" wokół glow)
3. statyczny **PNG** z alfą (bez animacji) — też podepnę, gdyby animacji nie było

Pętla krótka i bezszwowa (~2–3 s, ~12–24 klatki). Jak masz tylko statyczny obraz —
też dobry, animację mogę dorobić po stronie silnika.

---

## WKLEJ TEN BLOK PRZED KAŻDYM PROMPTEM (wspólny styl + wymogi techniczne)

```
A muscular cyberpunk GORILLA — the "WDGWars" mascot — as a single full-body character,
centered, vertical portrait, head-to-toe with a small margin. Dark techwear / hooded
jacket with glowing neon trim. Consistent character, outfit and proportions across the
whole set. Neon palette ONLY: cyan #22D3DB, magenta #FF2E88, electric violet #B23BD6,
ice-blue #82DCFF, white highlights; darkest shadows in deep navy/charcoal, never pure
black on the body. Clean crisp silhouette, strong rim light, subtle hologram/scanline
feel. ctOS / Watch Dogs hacker vibe.

BACKGROUND: fully TRANSPARENT (real alpha channel) — nothing behind the gorilla: no
scene, no floor, no ground shadow plate, no text, no logos, no UI, no frame.

OUTPUT: a short, seamlessly LOOPING animation (~2-3 s), transparent background preserved
in every frame. Prefer animated WebP or APNG for soft alpha. Keep the gorilla in place
(feet planted); only the action moves. Tall portrait, high resolution.
```

---

## 1) SCAN  — „Zwiadowca"  (plik: scan)
```
The gorilla stands calm and alert and SCANS the area: it raises a glowing handheld
scanner / phone in one hand, a soft teal radar ring pulses outward from the device,
its head turns slightly side to side tracking signals, ears/antenna twitching. Loop:
radar ring expands and fades, head sweeps left-right. Teal #22D3DB dominant.
```

## 2) SNIFF — „Podsłuch"  (plik: sniff)
```
The gorilla intercepts traffic: big over-ear headphones on, one hand pressed to the
ear-cup, head tilted LISTENING, a slim antenna on the hood. Faint packets of light
drift past and get "pulled in" toward the headphones. Loop: data packets stream by and
absorb, subtle nod. Electric violet + blue dominant, white highlights.
```

## 3) ATTACK — „Napastnik"  (plik: attack)
```
The gorilla UNLEASHES an attack: leans forward, pounds one fist into the open palm, a
burst of magenta energy / glitch shockwave radiates from the hands, jacket edges
datamosh-glitch for a frame. A half-mask over the muzzle. Loop: wind-up, fist-pound,
energy burst, settle. Magenta #FF2E88 dominant, with hot red-pink flashes.
```

## 4) ADDONS — „Majsterkowicz"  (plik: addons)
```
The gorilla shows off GADGETS: a tool-vest covered in small devices, a tiny quadcopter
drone hovers and circles near its shoulder, it flips a glowing multitool / Flipper-like
device in one hand, LEDs blink. Loop: drone orbits once, tool flips, LEDs pulse. Ice-blue
#82DCFF + cyan with small magenta accents.
```

## 5) SYSTEM — „Rdzeń ctOS / Operator"  (plik: system)  — POPRAWIONY, self-contained
```
FULL-BODY character, shown HEAD TO TOE: the entire gorilla is visible from the top of its
head down to the soles of its feet, standing upright and centered, with empty margin ABOVE
the head and BELOW the feet. This is a WIDE full-figure shot — NOT a portrait, NOT a bust,
NOT waist-up, NOT a close-up. Do not crop any part of the body.

BACKGROUND: 100% TRANSPARENT (real PNG alpha). There is NO background at all — no scene, no
control room, no floor, no ground, no drop shadow, no colored fill, no gradient, no text, no
frame. Only the gorilla and its floating holographic panels, cut out on empty transparency.

CHARACTER: a muscular cyberpunk GORILLA — the "WDGWars" mascot — one single full-body figure
in dark techwear with glowing neon trim; same outfit and proportions as the rest of the set.
Neon palette: cyan #22D3DB, magenta #FF2E88, electric violet #B23BD6, ice-blue #82DCFF, white
highlights; deep navy/charcoal shadows, never pure black. Crisp silhouette, strong rim light.

ACTION — SYSTEM / ctOS core operator: hood down, a thin AR visor glowing across the eyes. A few
translucent holographic panels float AROUND the standing gorilla (kept close to the body so the
whole figure still fits): a diagnostics readout, a power/rail toggle row, a slowly filling
progress ring, and a central pulsing core orb at chest height. Feet planted. Loop (~2-3 s): it
swipes one panel aside, flips a power toggle (a rail lights up green-cyan), the core orb pulses
once, the visor flickers, status lights breathe green. Cool cyan + white, clinical and
commanding — the most neutral / least neon of the set.

OUTPUT: a short seamlessly LOOPING animation (animated WebP or APNG for soft alpha), the
TRANSPARENT background preserved in EVERY frame, and the FULL BODY (head to feet) visible in
EVERY frame. Tall full-figure framing, high resolution.

AVOID: cropping at the waist or chest, headshot, bust, close-up on face or hands, any opaque or
colored background, floor or ground shadow, oversized panels that push the body out of frame,
text, logos, UI, borders.
```

## 6) EXTRAS — „Twórca modów"  (plik: extras)  — self-contained
```
FULL-BODY character, shown HEAD TO TOE: the entire gorilla is visible from the top of its head
down to the soles of its feet, standing upright and centered, with empty margin ABOVE the head
and BELOW the feet. A WIDE full-figure shot — NOT a portrait, NOT a bust, NOT waist-up, NOT a
close-up. Do not crop any part of the body.

BACKGROUND: 100% TRANSPARENT (real PNG alpha). NO background at all — no scene, no floor, no
ground, no drop shadow, no colored fill, no gradient, no text, no frame. Only the gorilla and its
floating app tiles, cut out on empty transparency (sticker / cut-out style).

CHARACTER: a muscular cyberpunk GORILLA — the "WDGWars" mascot — one single full-body figure in
dark techwear with glowing neon trim; same outfit and proportions as the rest of the set. Neon
palette: cyan #22D3DB, magenta #FF2E88, electric violet #B23BD6, ice-blue #82DCFF, white
highlights; deep navy/charcoal shadows, never pure black. Crisp silhouette, strong rim light.

ACTION — EXTRAS / maker of mini-apps: a small grid of glowing holographic app tiles floats and
orbits CLOSE around the standing gorilla like a launcher. It picks one tile out of the air and
SLOTS a glowing cartridge-like module into a port on its forearm; the chosen app icon lights up
and the tile snaps into the orbiting grid. Feet planted. Loop (~2-3 s): reach up, grab a tile,
plug the module in, it powers on with a soft pulse, the grid rotates one notch. Electric violet
#B23BD6 dominant (EXTRAS accent) + cyan. About SOFTWARE mini-apps/plugins (not hardware gadgets).

OUTPUT: a short seamlessly LOOPING animation (animated WebP or APNG for soft alpha), the
TRANSPARENT background preserved in EVERY frame, and the FULL BODY (head to feet) visible in
EVERY frame. Tall full-figure framing, high resolution.

AVOID: cropping at the waist or chest, headshot, bust, close-up on face or hands, any opaque or
colored background, floor or ground shadow, tiles so big they push the body out of frame, text,
logos, UI, borders.
```

## 7) CRISIS — „Tryb kryzysowy / Ocalały"  (plik: crisis)  — self-contained
```
FULL-BODY character, shown HEAD TO TOE: the entire gorilla is visible from the top of its head
down to the soles of its feet, standing upright and centered, with empty margin ABOVE the head
and BELOW the feet. A WIDE full-figure shot — NOT a portrait, NOT a bust, NOT waist-up, NOT a
close-up. Do not crop any part of the body.

BACKGROUND: 100% TRANSPARENT (real PNG alpha). NO background at all — no scene, no room, no
floor, no ground, no drop shadow, no colored fill, no gradient, no text, no frame. Only the
gorilla, cut out on empty transparency (sticker / cut-out style).

CHARACTER: a muscular cyberpunk GORILLA — the "WDGWars" mascot — one single full-body figure in
dark techwear with glowing neon trim; same outfit and proportions as the rest of the set. Neon
palette: cyan #22D3DB, magenta #FF2E88, electric violet #B23BD6, ice-blue #82DCFF, white
highlights; deep navy/charcoal shadows, never pure black. Crisp silhouette, strong rim light.

ACTION — CRISIS / survival mode: hood up against the cold, the gorilla holds up a rugged handheld
showing a glowing OFFLINE MAP with a compass needle swinging to north; a short antenna pulses a
slow EMERGENCY beacon while it listens for a distress signal, head steady and watchful, a faint
protective aura. Feet planted. Loop (~2-3 s): the compass needle swings and settles north, the
beacon blinks twice, a soft signal ripple passes outward. Ice-blue #82DCFF dominant with amber/red
emergency-beacon flashes. Calm, resilient, "lifeline" feel.

OUTPUT: a short seamlessly LOOPING animation (animated WebP or APNG for soft alpha), the
TRANSPARENT background preserved in EVERY frame, and the FULL BODY (head to feet) visible in
EVERY frame. Tall full-figure framing, high resolution.

AVOID: cropping at the waist or chest, headshot, bust, close-up on face or hands, any opaque or
colored background, floor or ground shadow, text, logos, UI, borders.
```

---

## Co zrobię po Twojej stronie
- Podepnę pliki per zakładka (`scan`→SCAN, `sniff`→SNIFF, itd.), postać zmieni się przy
  przełączaniu zakładek w menu.
- Jeśli to animacja (WebP/APNG/GIF): rozłożę na klatki i będę je przewijać w pętli w UI
  (postać „żyje" i robi swoją akcję). Jeśli statyczny PNG — dołożę subtelny hologram-ruch
  po stronie silnika.
- Alfa zostaje — w nowym UI (raylib) rysuję postać z prawdziwą przezroczystością.
```

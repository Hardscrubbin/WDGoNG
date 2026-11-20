/* LSP KEY - hardware signing dongle (LilyGO T-Dongle-C5), DEV build.
 *
 * A universal signing key; its first and only purpose today is the WatchDogsGo
 * extras catalogue. It builds the OpenSSH "SSHSIG" blob itself with a fixed
 * namespace, so the key cannot be used to sign anything else (no logins, no
 * commits). New purposes would be added with a firmware update - there is no
 * "sign arbitrary bytes" path.
 *
 * It does not take the host's word for what is being signed: over Wi-Fi it
 * downloads the published catalogue (index.json) and the candidate
 * (pending/index.json) from the hub, shows the difference on its own screen, and
 * only signs a digest that matches the candidate IT fetched - after confirmation.
 *
 * Optional PIN: when set, the P-256 private key is kept XOR-encrypted under
 * PBKDF2(PIN) and only unwrapped per signature, after the PIN is typed ON THE
 * DEVICE with the single button (short press = +1, long press = accept digit).
 * The PIN never travels over USB. Five wrong PINs lock signing until reboot.
 *
 * DEV build: without a PIN the key sits in NVS in the clear and the screen says
 * DEV. The production build keeps the key in an eFuse ECDSA block behind secure
 * boot. The screen and the dashboard say which mode is active.
 *
 * Host protocol (USB serial, one line each way; replies start with '@'; SSIDs,
 * device name and such travel as hex):
 *   info | name <hex> | flip
 *   wifi clear | wifi add <ssid-hex> <pass-hex> | wifi del <n> | wifi list
 *   hub <url> | check | sign <sha512-hex>
 *   pin set <4-8> | pin clear          (digits are entered on the device)
 *   export | export done               (one-time backup: PIN on the device, then locked)
 *   import <scalar-hex>                (restore a backup; replaces the key, locks export)
 */
#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "driver/gpio.h"
#include "driver/usb_serial_jtag.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/ecdsa.h"
#include "mbedtls/ecp.h"
#include "mbedtls/pkcs5.h"
#include "mbedtls/sha256.h"
#include "mbedtls/sha512.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "lcd.h"

#define FW_VERSION "0.4-dev"
#define NAMESPACE "wdgo-extras-hub"
#define PIN_BTN 28
#define MAX_NETS 6
#define MAX_DOC (48 * 1024)
#define MAX_ITEMS 400
#define MAX_CHANGES 6
#define CHECK_PERIOD_S 300
#define PIN_ITER 20000
#define PIN_MAX_FAILS 5
#define LONG_PRESS_MS 700
#define CONFIRM_WAIT_S 180          /* how long a PIN / button prompt waits for the next press */

typedef struct { char ssid[33]; char pass[65]; } net_t;
typedef struct { char id[33]; char ver[17]; char sha[65]; uint8_t perms; } item_t;

static net_t s_nets[MAX_NETS];
static int s_nnets;
static char s_hub[160];
static char s_name[17] = "LSP KEY";      /* shown on the screen; set from the host */
static int s_flip;
static mbedtls_ecp_keypair s_key;        /* loaded in the clear only while there is NO PIN */
static uint8_t s_pub[65];
static uint8_t s_pin_len;                /* 0 = no PIN; else number of digits */
static uint8_t s_pin_fails;
static uint8_t s_exported;               /* 1 = the one-time key export is used up */
static char s_ui_note[27];               /* a one-shot red line on the main screen */

static EventGroupHandle_t s_ev;
#define EV_IP BIT0
static char s_ssid[33];
static volatile int s_connecting;
static SemaphoreHandle_t s_lock;

/* result of the last hub check */
static enum { HUB_NONE, HUB_NOWIFI, HUB_ERROR, HUB_OK } s_hub_state = HUB_NONE;
static char s_hub_err[28];
static uint8_t s_pending_sha[64];
static int s_n_changes;
static char s_changes[MAX_CHANGES][27];
static volatile int s_want_check = 1;
static volatile int s_prompt;            /* 1 = device owns the screen (confirm / PIN entry) */
static int s_prompt_verified;
static int s_prompt_left;
static char s_prompt_fp[17];

/* ---------------------------------------------------------------- utils */
static int rng(void *ctx, unsigned char *buf, size_t len)
{
    (void)ctx;
    esp_fill_random(buf, len);
    return 0;
}

static int hexval(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    c = tolower(c);
    return (c >= 'a' && c <= 'f') ? c - 'a' + 10 : -1;
}

static int unhex(const char *s, uint8_t *out, size_t max)
{
    size_t n = strlen(s);
    if (n % 2 || n / 2 > max) return -1;
    for (size_t i = 0; i < n / 2; i++) {
        int a = hexval(s[2 * i]), b = hexval(s[2 * i + 1]);
        if (a < 0 || b < 0) return -1;
        out[i] = (uint8_t)(a << 4 | b);
    }
    return (int)(n / 2);
}

static void hex(const uint8_t *d, size_t n, char *out)
{
    static const char H[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = H[d[i] >> 4];
        out[2 * i + 1] = H[d[i] & 15];
    }
    out[2 * n] = 0;
}

static void reply(const char *fmt, ...)
{
    char buf[400];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf + 1, sizeof(buf) - 3, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if (n > (int)sizeof(buf) - 4) n = sizeof(buf) - 4;
    buf[0] = '@';
    buf[n + 1] = '\n';
    usb_serial_jtag_write_bytes(buf, n + 2, pdMS_TO_TICKS(200));
}

/* ---------------------------------------------------------------- storage */
static void store_blob(const char *k, const void *d, size_t n)
{
    nvs_handle_t h;
    if (nvs_open("hubkey", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_blob(h, k, d, n);
        nvs_commit(h);
        nvs_close(h);
    }
}

static size_t load_blob(const char *k, void *d, size_t max)
{
    nvs_handle_t h;
    size_t n = max;
    if (nvs_open("hubkey", NVS_READONLY, &h) != ESP_OK) return 0;
    if (nvs_get_blob(h, k, d, &n) != ESP_OK) n = 0;
    nvs_close(h);
    return n;
}

/* ---------------------------------------------------------------- key / PIN */
/* Load a raw 32-byte scalar into kp, derive the public point, and (if we already
 * know our public key) reject anything that does not match - that is how a wrong
 * PIN is detected. 0 = ok, -1 = not our key / invalid. */
static int load_plain(mbedtls_ecp_keypair *kp, const uint8_t d[32])
{
    uint8_t pub[65];
    size_t olen = 0;
    mbedtls_ecp_keypair_init(kp);
    if (mbedtls_ecp_read_key(MBEDTLS_ECP_DP_SECP256R1, kp, d, 32) != 0 ||
        mbedtls_ecp_keypair_calc_public(kp, rng, NULL) != 0 ||
        mbedtls_ecp_write_public_key(kp, MBEDTLS_ECP_PF_UNCOMPRESSED, &olen, pub, sizeof(pub)) != 0) {
        mbedtls_ecp_keypair_free(kp);
        return -1;
    }
    if (s_pub[0] == 0x04 && memcmp(pub, s_pub, 65)) {      /* not our key -> wrong PIN */
        mbedtls_ecp_keypair_free(kp);
        return -1;
    }
    memcpy(s_pub, pub, 65);
    return 0;
}

static void pin_stream(const char *pin, const uint8_t salt[16], uint8_t out[32])
{
    mbedtls_pkcs5_pbkdf2_hmac_ext(MBEDTLS_MD_SHA256, (const unsigned char *)pin, strlen(pin),
                                  salt, 16, PIN_ITER, 32, out);
}

/* Unlock the PIN-wrapped key into kp. 0 = ok, -1 = wrong PIN, -2 = locked out. */
static int key_unlock(const char *pin, mbedtls_ecp_keypair *kp)
{
    uint8_t enc[32], salt[16], k[32], d[32];
    if (s_pin_fails >= PIN_MAX_FAILS) return -2;
    if (load_blob("priv", enc, 32) != 32 || load_blob("pinsalt", salt, 16) != 16) return -1;
    pin_stream(pin, salt, k);
    for (int i = 0; i < 32; i++) d[i] = enc[i] ^ k[i];
    int rc = load_plain(kp, d);
    memset(k, 0, sizeof(k));
    memset(d, 0, sizeof(d));
    s_pin_fails = rc == 0 ? 0 : s_pin_fails + 1;
    store_blob("pinfail", &s_pin_fails, 1);
    return rc;
}

/* Re-store the raw key XOR-encrypted under a fresh salt derived from `pin`. */
static void key_wrap(const uint8_t d[32], const char *pin, int len)
{
    uint8_t salt[16], k[32], enc[32];
    esp_fill_random(salt, sizeof(salt));
    pin_stream(pin, salt, k);
    for (int i = 0; i < 32; i++) enc[i] = d[i] ^ k[i];
    store_blob("pinsalt", salt, 16);
    store_blob("priv", enc, 32);
    s_pin_len = (uint8_t)len;
    s_pin_fails = 0;
    store_blob("pinlen", &s_pin_len, 1);
    store_blob("pinfail", &s_pin_fails, 1);
    memset(k, 0, sizeof(k));
}

static void key_init(void)
{
    uint8_t d[32];
    size_t olen = 0;
    load_blob("pinlen", &s_pin_len, 1);
    load_blob("pinfail", &s_pin_fails, 1);
    load_blob("exported", &s_exported, 1);
    load_blob("pub", s_pub, sizeof(s_pub));
    if (s_pin_len) return;                               /* locked: only the public half is known */
    if (load_blob("priv", d, sizeof(d)) == 32 && load_plain(&s_key, d) == 0) {
        /* loaded in the clear */
    } else {
        memset(s_pub, 0, sizeof(s_pub));
        mbedtls_ecp_keypair_init(&s_key);
        mbedtls_ecp_gen_key(MBEDTLS_ECP_DP_SECP256R1, &s_key, rng, NULL);
        mbedtls_ecp_write_key_ext(&s_key, &olen, d, sizeof(d));
        store_blob("priv", d, 32);
        mbedtls_ecp_write_public_key(&s_key, MBEDTLS_ECP_PF_UNCOMPRESSED, &olen, s_pub, sizeof(s_pub));
    }
    store_blob("pub", s_pub, sizeof(s_pub));
    memset(d, 0, sizeof(d));
}

/* Sign the catalogue digest. The blob layout is OpenSSH's PROTOCOL.sshsig:
 *   "SSHSIG" string(namespace) string(reserved="") string("sha512") string(H(msg))
 * and for an ecdsa-sha2-nistp256 key the signature is ECDSA over SHA-256(blob). */
static int sign_digest(mbedtls_ecp_keypair *kp, const uint8_t h512[64], uint8_t r32[32], uint8_t s32[32])
{
    uint8_t blob[6 + 4 + sizeof(NAMESPACE) + 4 + 4 + 6 + 4 + 64], *p = blob, dig[32];
#define PUT_STR(ptr, len) do { uint32_t l_ = (len); *p++ = l_ >> 24; *p++ = l_ >> 16; *p++ = l_ >> 8; *p++ = l_; \
                               memcpy(p, (ptr), l_); p += l_; } while (0)
    memcpy(p, "SSHSIG", 6); p += 6;
    PUT_STR(NAMESPACE, strlen(NAMESPACE));
    PUT_STR("", 0);
    PUT_STR("sha512", 6);
    PUT_STR(h512, 64);
    mbedtls_sha256(blob, p - blob, dig, 0);

    mbedtls_ecp_group grp;
    mbedtls_mpi d, r, s;
    mbedtls_ecp_point q;
    mbedtls_ecp_group_init(&grp); mbedtls_mpi_init(&d); mbedtls_mpi_init(&r); mbedtls_mpi_init(&s);
    mbedtls_ecp_point_init(&q);
    int rc = mbedtls_ecp_export(kp, &grp, &d, &q);
    if (rc == 0) rc = mbedtls_ecdsa_sign(&grp, &r, &s, &d, dig, sizeof(dig), rng, NULL);
    if (rc == 0) rc = mbedtls_mpi_write_binary(&r, r32, 32);
    if (rc == 0) rc = mbedtls_mpi_write_binary(&s, s32, 32);
    mbedtls_ecp_group_free(&grp); mbedtls_mpi_free(&d); mbedtls_mpi_free(&r); mbedtls_mpi_free(&s);
    mbedtls_ecp_point_free(&q);
    return rc;
}

/* ---------------------------------------------------------------- wifi */
static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_ev, EV_IP);
        if (s_connecting > 0) {          /* a stale or first-try disconnect: just try again */
            s_connecting--;
            esp_wifi_connect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(s_ev, EV_IP);
    }
}

static void wifi_init(void)
{
    esp_netif_init();
    esp_event_loop_create_default();
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi, NULL);
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_start();
}

static int wifi_up(void)
{
    if (xEventGroupGetBits(s_ev) & EV_IP) {
        wifi_ap_record_t ap;
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) strlcpy(s_ssid, (const char *)ap.ssid, sizeof(s_ssid));
        return 1;
    }
    for (int i = 0; i < s_nnets; i++) {
        wifi_config_t wc = {0};
        strlcpy((char *)wc.sta.ssid, s_nets[i].ssid, sizeof(wc.sta.ssid));
        strlcpy((char *)wc.sta.password, s_nets[i].pass, sizeof(wc.sta.password));
        s_connecting = 0;
        esp_wifi_disconnect();
        vTaskDelay(pdMS_TO_TICKS(300));
        xEventGroupClearBits(s_ev, EV_IP);
        esp_wifi_set_config(WIFI_IF_STA, &wc);
        s_connecting = 4;
        esp_wifi_connect();
        EventBits_t b = xEventGroupWaitBits(s_ev, EV_IP, pdFALSE, pdFALSE, pdMS_TO_TICKS(15000));
        s_connecting = 0;
        if (b & EV_IP) {
            strlcpy(s_ssid, s_nets[i].ssid, sizeof(s_ssid));
            return 1;
        }
    }
    s_ssid[0] = 0;
    return 0;
}

/* ---------------------------------------------------------------- hub check */
static char *fetch(const char *path, int *len, int *status)
{
    char url[256];
    snprintf(url, sizeof(url), "%s/%s", s_hub, path);
    esp_http_client_config_t cfg = {.url = url, .timeout_ms = 12000,
                                    .crt_bundle_attach = esp_crt_bundle_attach};
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    char *buf = NULL;
    *len = 0;
    *status = 0;
    if (!c) return NULL;
    if (esp_http_client_open(c, 0) == ESP_OK) {
        esp_http_client_fetch_headers(c);
        *status = esp_http_client_get_status_code(c);
        if (*status == 200 && (buf = malloc(MAX_DOC + 1)) != NULL) {
            int n = 0, r;
            while (n < MAX_DOC && (r = esp_http_client_read(c, buf + n, MAX_DOC - n)) > 0) n += r;
            if (n >= MAX_DOC) { free(buf); buf = NULL; *status = 413; }
            else { buf[n] = 0; *len = n; }
        }
    }
    esp_http_client_cleanup(c);
    return buf;
}

static uint8_t perm_bits(const cJSON *arr)
{
    uint8_t b = 0;
    const cJSON *p;
    cJSON_ArrayForEach(p, arr) {
        if (!cJSON_IsString(p)) continue;
        if (!strcmp(p->valuestring, "gps")) b |= 1;
        else if (!strcmp(p->valuestring, "loot_read")) b |= 2;
        else if (!strcmp(p->valuestring, "loot_write")) b |= 4;
        else b |= 0x80;                                   /* something this build does not know */
    }
    return b;
}

static int parse_items(const char *json, item_t **out)
{
    cJSON *root = cJSON_Parse(json);
    const cJSON *ex = root ? cJSON_GetObjectItem(root, "extras") : NULL, *e;
    int n = 0;
    *out = NULL;
    if (!cJSON_IsObject(ex)) { cJSON_Delete(root); return -1; }
    int cap = cJSON_GetArraySize(ex);
    if (cap > MAX_ITEMS) cap = MAX_ITEMS;
    item_t *it = calloc(cap ? cap : 1, sizeof(item_t));
    if (!it) { cJSON_Delete(root); return -2; }
    cJSON_ArrayForEach(e, ex) {
        if (n >= cap || !e->string) break;
        const cJSON *v = cJSON_GetObjectItem(e, "version"), *s = cJSON_GetObjectItem(e, "sha");
        strlcpy(it[n].id, e->string, sizeof(it[n].id));
        strlcpy(it[n].ver, cJSON_IsString(v) ? v->valuestring : "?", sizeof(it[n].ver));
        strlcpy(it[n].sha, cJSON_IsString(s) ? s->valuestring : "", sizeof(it[n].sha));
        it[n].perms = perm_bits(cJSON_GetObjectItem(e, "permissions"));
        n++;
    }
    cJSON_Delete(root);
    *out = it;
    return n;
}

static void add_change(char mark, const char *id, const char *ver)
{
    if (s_n_changes < MAX_CHANGES) {
        snprintf(s_changes[s_n_changes], sizeof(s_changes[0]), "%c%.15s %.8s", mark, id, ver);
    }
    s_n_changes++;
}

static void hub_check(void)
{
    item_t *cur = NULL, *pen = NULL;
    int ncur = 0, npen = 0, len, st;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_n_changes = 0;
    s_hub_err[0] = 0;
    if (!s_hub[0]) { s_hub_state = HUB_NONE; goto out; }
    if (!wifi_up()) { s_hub_state = HUB_NOWIFI; goto out; }
    char *doc = fetch("index.json", &len, &st);
    if (doc) {
        ncur = parse_items(doc, &cur);
        free(doc);
        if (ncur < 0) { snprintf(s_hub_err, sizeof(s_hub_err), ncur == -2 ? "brak pamieci" : "index zly json"); s_hub_state = HUB_ERROR; goto out; }
    } else if (st != 404) { snprintf(s_hub_err, sizeof(s_hub_err), "index http %d", st); s_hub_state = HUB_ERROR; goto out; }
    doc = fetch("pending/index.json", &len, &st);
    if (!doc) { snprintf(s_hub_err, sizeof(s_hub_err), "pending http %d", st); s_hub_state = HUB_ERROR; goto out; }
    mbedtls_sha512((const unsigned char *)doc, len, s_pending_sha, 0);
    npen = parse_items(doc, &pen);
    free(doc);
    if (npen < 0) { strlcpy(s_hub_err, npen == -2 ? "brak pamieci" : "pending zly json", sizeof(s_hub_err)); s_hub_state = HUB_ERROR; goto out; }
    for (int i = 0; i < npen; i++) {
        int j;
        for (j = 0; j < ncur && strcmp(cur[j].id, pen[i].id); j++) {}
        if (j == ncur) add_change('+', pen[i].id, pen[i].ver);
        else if (strcmp(cur[j].sha, pen[i].sha)) {
            add_change((pen[i].perms & ~cur[j].perms) ? '!' : '~', pen[i].id, pen[i].ver);
        }
    }
    for (int j = 0; j < ncur; j++) {
        int i;
        for (i = 0; i < npen && strcmp(cur[j].id, pen[i].id); i++) {}
        if (i == npen) add_change('-', cur[j].id, "");
    }
    s_hub_state = HUB_OK;
out:
    free(cur);
    free(pen);
    xSemaphoreGive(s_lock);
}

/* ---------------------------------------------------------------- screen */
static void header(const char *left, const char *right)
{
    lcd_rect(0, 0, LCD_W, 13, C_MANGO);
    lcd_text(3, 2, left, C_BLACK, 1);
    if (right) lcd_text(LCD_W - 6 * (int)strlen(right) - 3, 2, right, C_BLACK, 1);
}

static void draw(void)
{
    char l[40];
    lcd_clear(C_BLACK);
    if (s_prompt) {                                       /* a confirm/PIN screen owns the display */
        lcd_flush();
        return;
    }
    header(s_name, s_pin_len ? "DEV+PIN" : "DEV");
    if (s_ssid[0]) snprintf(l, sizeof(l), "wifi %.20s", s_ssid);
    else snprintf(l, sizeof(l), s_nnets ? "wifi: brak polaczenia" : "wifi: brak sieci");
    lcd_text(3, 16, l, C_DIM, 1);
    switch (s_hub_state) {
    case HUB_NONE: lcd_text(3, 28, "hub: nie ustawiony", C_MANGO, 1); break;
    case HUB_NOWIFI: lcd_text(3, 28, "hub: brak wifi", C_MANGO, 1); break;
    case HUB_ERROR: snprintf(l, sizeof(l), "hub: %.20s", s_hub_err); lcd_text(3, 28, l, C_RED, 1); break;
    case HUB_OK:
        if (!s_n_changes) {
            lcd_text(3, 28, "nic do podpisania", C_GREEN, 1);
        } else {
            char fp[9];
            hex(s_pending_sha, 4, fp);
            snprintf(l, sizeof(l), "DO PODPISU %d fp %s", s_n_changes, fp);
            lcd_text(3, 28, l, C_MANGO, 1);
            for (int i = 0; i < s_n_changes && i < (s_ui_note[0] ? 2 : 4); i++) {
                uint16_t c = s_changes[i][0] == '!' ? C_RED : s_changes[i][0] == '-' ? C_DIM : C_WHITE;
                lcd_text(3, 40 + i * 10, s_changes[i], c, 1);
            }
        }
        break;
    }
    if (s_ui_note[0]) lcd_text(3, 66, s_ui_note, C_RED, 1);
    lcd_flush();
}

static void note(const char *t)
{
    strlcpy(s_ui_note, t, sizeof(s_ui_note));
}

/* Hold until the button is released, so an earlier press does not count twice. */
static void wait_release(void)
{
    while (gpio_get_level(PIN_BTN) == 0) vTaskDelay(pdMS_TO_TICKS(20));
}

/* PIN entry with the single button: short press = digit +1 (0-9 wrap), long press
 * = accept the digit and advance. The PIN never passes through the computer.
 * Returns 1 when `len` digits were entered, 0 after CONFIRM_WAIT_S without a press. The caller
 * owns the screen (s_prompt must be set). */
static int pin_entry(const char *title, const char *sub, int len, char *out)
{
    int pos = 0, digit = 0;
    wait_release();
    for (;;) {
        lcd_clear(C_BLACK);
        header(title, NULL);
        if (sub) lcd_text(3, 16, sub, C_WHITE, 1);
        for (int i = 0; i < len; i++) {
            char c[2] = {i < pos ? '*' : i == pos ? (char)('0' + digit) : '_', 0};
            lcd_text(8 + i * 18, 30, c, i == pos ? C_MANGO : C_DIM, 2);
        }
        lcd_text(3, 66, "krotko:+1  dlugo:dalej", C_DIM, 1);
        lcd_flush();
        int idle = 0;
        while (gpio_get_level(PIN_BTN) == 1) {
            vTaskDelay(pdMS_TO_TICKS(20));
            if (++idle > CONFIRM_WAIT_S * 50) return 0;  /* no press for CONFIRM_WAIT_S */
        }
        int held = 0;
        while (gpio_get_level(PIN_BTN) == 0) {
            vTaskDelay(pdMS_TO_TICKS(20));
            held += 20;
            if (held == LONG_PRESS_MS) {                 /* a long press is now committed: show it */
                lcd_rect(8 + pos * 18, 52, 12, 3, C_GREEN);
                lcd_flush();
            }
        }
        if (held < 40) continue;                         /* debounce a glitch */
        if (held >= LONG_PRESS_MS) {
            out[pos++] = (char)('0' + digit);
            digit = 0;
            if (pos == len) { out[len] = 0; return 1; }
        } else {
            digit = (digit + 1) % 10;
        }
    }
}

/* ---------------------------------------------------------------- commands */
static void save_nets(void) { store_blob("wifi", s_nets, sizeof(net_t) * s_nnets); }

/* Plain button confirmation (no PIN): fresh press within CONFIRM_WAIT_S. 1 = confirmed. */
static int confirm_button(void)
{
    wait_release();
    for (int t = CONFIRM_WAIT_S; t > 0; t--) {      /* one pass = one second */
        char l[40];
        s_prompt_left = t;
        lcd_clear(C_BLACK);
        header("PODPISAC WYDANIE?", NULL);
        snprintf(l, sizeof(l), "fp %s", s_prompt_fp);
        lcd_text(3, 17, l, C_WHITE, 1);
        snprintf(l, sizeof(l), "zmian: %d", s_n_changes);
        lcd_text(3, 29, l, C_CYAN, 1);
        lcd_text(3, 41, s_prompt_verified ? "zgodne z hubem" : "NIE SPRAWDZONE z hubem",
                 s_prompt_verified ? C_GREEN : C_RED, 1);
        snprintf(l, sizeof(l), "PRZYCISK = TAK %3ds", s_prompt_left);
        lcd_text(3, 62, l, C_MANGO, 1);
        lcd_flush();
        for (int i = 0; i < 10; i++) {
            if (gpio_get_level(PIN_BTN) == 0) {
                vTaskDelay(pdMS_TO_TICKS(40));
                if (gpio_get_level(PIN_BTN) == 0) return 1;
            }
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
    return 0;
}

static void cmd_sign(const char *arg)
{
    uint8_t h[64], r[32], s[32];
    char rh[65], sh[65], sub[27];
    if (unhex(arg, h, 64) != 64) { reply("ERR sign needs a sha512 hex digest"); return; }
    hub_check();
    int verified = s_hub_state == HUB_OK;
    if (verified && memcmp(h, s_pending_sha, 64)) {
        reply("ERR digest does not match the hub's pending catalogue - refusing");
        return;
    }
    hex(h, 8, s_prompt_fp);
    s_prompt_verified = verified;
    note("");
    mbedtls_ecp_keypair unlocked, *kp = &s_key;
    int have_unlocked = 0;
    s_prompt = 1;
    if (s_pin_len) {
        if (s_pin_fails >= PIN_MAX_FAILS) { s_prompt = 0; note("PIN ZABLOKOWANY"); draw(); reply("ERR pin locked - reboot the device"); return; }
        snprintf(sub, sizeof(sub), "%s %.16s", verified ? "hub OK" : "NIESPR.", s_prompt_fp);
        char pin[9];
        int got = pin_entry("PIN = PODPIS", sub, s_pin_len, pin);
        if (!got) { s_prompt = 0; draw(); reply("ERR not confirmed on the device"); return; }
        int rc = key_unlock(pin, &unlocked);
        memset(pin, 0, sizeof(pin));
        if (rc != 0) {
            s_prompt = 0;
            snprintf(sub, sizeof(sub), rc == -2 ? "PIN ZABLOKOWANY" : "ZLY PIN (%d/%d)", s_pin_fails, PIN_MAX_FAILS);
            note(sub); draw();
            reply("ERR wrong pin (%d/%d)", s_pin_fails, PIN_MAX_FAILS);
            return;
        }
        kp = &unlocked;
        have_unlocked = 1;
    } else {
        int ok = confirm_button();
        if (!ok) { s_prompt = 0; draw(); reply("ERR not confirmed on the device"); return; }
    }
    s_prompt = 0;
    int rc = sign_digest(kp, h, r, s);
    if (have_unlocked) mbedtls_ecp_keypair_free(&unlocked);
    draw();
    if (rc != 0) { reply("ERR signing failed"); return; }
    hex(r, 32, rh);
    hex(s, 32, sh);
    reply("OK sig %s %s %s", rh, sh, verified ? "verified" : "unverified");
}

/* pin set <len> : (current PIN if one is set) then the new PIN twice, on the device.
 * pin clear     : current PIN, then the key is stored unwrapped again. */
static void cmd_pin(const char *arg)
{
    uint8_t d[32];
    size_t olen = 0;
    char a[9], b[9];
    int setting = !strncmp(arg, "set", 3), len = setting ? atoi(arg + 3) : 0;
    if (!setting && strcmp(arg, "clear")) { reply("ERR pin set <4-8> | pin clear"); return; }
    if (setting && (len < 4 || len > 8)) { reply("ERR pin length 4-8"); return; }
    if (!setting && !s_pin_len) { reply("OK pin not set"); return; }
    note("");
    s_prompt = 1;
    if (s_pin_len) {                                      /* must prove the current PIN first */
        mbedtls_ecp_keypair cur;
        if (s_pin_fails >= PIN_MAX_FAILS) { s_prompt = 0; note("PIN ZABLOKOWANY"); draw(); reply("ERR pin locked - reboot"); return; }
        if (!pin_entry("OBECNY PIN", NULL, s_pin_len, a)) { s_prompt = 0; draw(); reply("ERR timeout"); return; }
        if (key_unlock(a, &cur) != 0) {
            s_prompt = 0; note("ZLY PIN"); draw();
            reply("ERR wrong pin (%d/%d)", s_pin_fails, PIN_MAX_FAILS);
            return;
        }
        olen = 0;
        mbedtls_ecp_write_key_ext(&cur, &olen, d, sizeof(d));
        mbedtls_ecp_keypair_free(&cur);
    } else {                                              /* no PIN yet: key is in the clear */
        mbedtls_ecp_write_key_ext(&s_key, &olen, d, sizeof(d));
    }
    if (setting) {
        if (!pin_entry("NOWY PIN", NULL, len, a) || !pin_entry("POWTORZ PIN", NULL, len, b)) {
            s_prompt = 0; memset(d, 0, sizeof(d)); draw(); reply("ERR timeout"); return;
        }
        if (strcmp(a, b)) { s_prompt = 0; memset(d, 0, sizeof(d)); note("PIN-y rozne"); draw(); reply("ERR the two entries differ"); return; }
        if (!s_pin_len) mbedtls_ecp_keypair_free(&s_key);
        key_wrap(d, a, len);
        reply("OK pin set (%d digits)", len);
    } else {                                              /* clear: store the key in the clear again */
        store_blob("priv", d, 32);
        s_pin_len = 0;
        store_blob("pinlen", &s_pin_len, 1);
        load_plain(&s_key, d);
        reply("OK pin cleared");
    }
    memset(d, 0, sizeof(d));
    memset(a, 0, sizeof(a));
    memset(b, 0, sizeof(b));
    s_prompt = 0;
    draw();
}

/* A yes/no question answered with the button: fresh press within CONFIRM_WAIT_S = yes. */
static int confirm_simple(const char *title, const char *l1, const char *l2)
{
    wait_release();
    for (int t = CONFIRM_WAIT_S; t > 0; t--) {
        char l[40];
        lcd_clear(C_BLACK);
        header(title, NULL);
        lcd_text(3, 20, l1, C_WHITE, 1);
        lcd_text(3, 32, l2, C_RED, 1);
        snprintf(l, sizeof(l), "PRZYCISK = TAK %3ds", t);
        lcd_text(3, 62, l, C_MANGO, 1);
        lcd_flush();
        for (int i = 0; i < 10; i++) {
            if (gpio_get_level(PIN_BTN) == 0) {
                vTaskDelay(pdMS_TO_TICKS(40));
                if (gpio_get_level(PIN_BTN) == 0) { wait_release(); return 1; }
            }
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
    return 0;
}

/* export      : hand the raw private scalar to the host ONCE, for an encrypted backup.
 *               Needs a PIN to be set and typed on the device. The host must then
 *               send "export done"; from that moment export is refused for good
 *               (until a different key is imported or generated).
 * export done : burn the one-time export. */
static void cmd_export(const char *arg)
{
    uint8_t d[32];
    size_t olen = 0;
    char pin[9], dh[65];
    mbedtls_ecp_keypair kp;
    if (!strcmp(arg, "done")) {
        s_exported = 1;
        store_blob("exported", &s_exported, 1);
        reply("OK export locked");
        return;
    }
    if (s_exported) { reply("ERR export already used"); return; }
    if (!s_pin_len) { reply("ERR set a pin first"); return; }
    if (s_pin_fails >= PIN_MAX_FAILS) { note("PIN ZABLOKOWANY"); draw(); reply("ERR pin locked - reboot"); return; }
    note("");
    s_prompt = 1;
    int got = pin_entry("PIN = EKSPORT", "klucz opusci urzadzenie", s_pin_len, pin);
    if (!got) { s_prompt = 0; draw(); reply("ERR timeout"); return; }
    int rc = key_unlock(pin, &kp);
    memset(pin, 0, sizeof(pin));
    s_prompt = 0;
    if (rc != 0) { note("ZLY PIN"); draw(); reply("ERR wrong pin (%d/%d)", s_pin_fails, PIN_MAX_FAILS); return; }
    mbedtls_ecp_write_key_ext(&kp, &olen, d, sizeof(d));
    mbedtls_ecp_keypair_free(&kp);
    hex(d, 32, dh);
    memset(d, 0, sizeof(d));
    draw();
    reply("OK key %s", dh);
    memset(dh, 0, sizeof(dh));
}

/* import <hex> : replace this device's key with one from a backup. With a PIN set,
 * the current PIN is asked and the new key is wrapped under it; without one, the
 * button confirms. Export is locked afterwards - the backup already exists. */
static void cmd_import(const char *arg)
{
    uint8_t d[32], old_pub[65];
    char pin[9] = "";
    mbedtls_ecp_keypair kp;
    if (unhex(arg, d, 32) != 32) { reply("ERR import needs a 32-byte scalar in hex"); return; }
    note("");
    s_prompt = 1;
    if (s_pin_len) {
        if (s_pin_fails >= PIN_MAX_FAILS) { s_prompt = 0; memset(d, 0, 32); note("PIN ZABLOKOWANY"); draw(); reply("ERR pin locked - reboot"); return; }
        if (!pin_entry("PIN = IMPORT", "obecny klucz zniknie", s_pin_len, pin)) { s_prompt = 0; memset(d, 0, 32); draw(); reply("ERR timeout"); return; }
        if (key_unlock(pin, &kp) != 0) {
            s_prompt = 0; memset(d, 0, 32); memset(pin, 0, sizeof(pin)); note("ZLY PIN"); draw();
            reply("ERR wrong pin (%d/%d)", s_pin_fails, PIN_MAX_FAILS);
            return;
        }
        mbedtls_ecp_keypair_free(&kp);
    } else if (!confirm_simple("IMPORT KLUCZA?", "zastapi klucz w urzadz.", "obecny klucz zniknie")) {
        s_prompt = 0; memset(d, 0, 32); draw(); reply("ERR not confirmed on the device");
        return;
    }
    memcpy(old_pub, s_pub, 65);
    memset(s_pub, 0, sizeof(s_pub));                      /* accept a different key in load_plain */
    if (load_plain(&kp, d) != 0) {
        memcpy(s_pub, old_pub, 65);
        s_prompt = 0; memset(d, 0, 32); memset(pin, 0, sizeof(pin)); draw();
        reply("ERR not a valid P-256 key");
        return;
    }
    if (s_pin_len) {
        mbedtls_ecp_keypair_free(&kp);
        key_wrap(d, pin, s_pin_len);
    } else {
        mbedtls_ecp_keypair_free(&s_key);
        s_key = kp;                                       /* struct copy: s_key now owns it */
        store_blob("priv", d, 32);
    }
    store_blob("pub", s_pub, sizeof(s_pub));
    s_exported = 1;
    store_blob("exported", &s_exported, 1);
    memset(d, 0, 32);
    memset(pin, 0, sizeof(pin));
    s_prompt = 0;
    draw();
    reply("OK imported");
}

static void handle(char *line)
{
    char *arg = strchr(line, ' ');
    if (arg) *arg++ = 0; else arg = "";
    if (!strcmp(line, "info")) {
        char ph[131], nh[33], wh[65];
        hex(s_pub, 65, ph);
        hex((const uint8_t *)s_name, strlen(s_name), nh);
        hex((const uint8_t *)s_ssid, strlen(s_ssid), wh);
        reply("OK info fw=%s mode=DEV pub=%s name=%s wifi=%s nets=%d ns=%s pin=%d locked=%d exp=%d hub=%s",
              FW_VERSION, ph, nh, wh[0] ? wh : "-", s_nnets, NAMESPACE, (int)s_pin_len,
              s_pin_fails >= PIN_MAX_FAILS, (int)s_exported, s_hub[0] ? s_hub : "-");
    } else if (!strcmp(line, "wifi") && !strcmp(arg, "clear")) {
        s_nnets = 0; save_nets(); reply("OK wifi cleared");
    } else if (!strcmp(line, "wifi") && !strcmp(arg, "list")) {
        char buf[6 * 66 + 1] = "", one[65];
        for (int i = 0; i < s_nnets; i++) {            /* SSIDs only, hex; passwords never leave */
            hex((const uint8_t *)s_nets[i].ssid, strlen(s_nets[i].ssid), one);
            strlcat(buf, one, sizeof(buf)); strlcat(buf, ";", sizeof(buf));
        }
        reply("OK wifi %s", buf);
    } else if (!strcmp(line, "wifi") && !strncmp(arg, "del ", 4)) {
        int i = atoi(arg + 4);
        if (i < 0 || i >= s_nnets) { reply("ERR no such network"); return; }
        memmove(&s_nets[i], &s_nets[i + 1], sizeof(net_t) * (s_nnets - i - 1));
        s_nnets--; save_nets();
        reply("OK wifi deleted (%d stored)", s_nnets);
    } else if (!strcmp(line, "wifi") && !strncmp(arg, "add ", 4)) {
        char *a = arg + 4, *b = strchr(a, ' ');
        if (!b || s_nnets >= MAX_NETS) { reply("ERR wifi add <ssid-hex> <pass-hex> (max %d)", MAX_NETS); return; }
        *b++ = 0;
        net_t n = {0};
        int ls = unhex(a, (uint8_t *)n.ssid, 32), lp = unhex(b, (uint8_t *)n.pass, 64);
        if (ls <= 0 || lp < 0) { reply("ERR bad hex"); return; }
        s_nets[s_nnets++] = n; save_nets(); s_want_check = 1;
        reply("OK wifi added (%d stored)", s_nnets);
    } else if (!strcmp(line, "name")) {
        uint8_t raw[17] = {0};
        int n = unhex(arg, raw, 16);
        if (n <= 0) { reply("ERR name <hex, 1-16 chars>"); return; }
        for (int i = 0; i < n; i++) if (raw[i] < 32 || raw[i] > 126) { reply("ERR name must be plain ASCII"); return; }
        memcpy(s_name, raw, n); s_name[n] = 0;
        store_blob("name", s_name, strlen(s_name) + 1); draw();
        reply("OK name set");
    } else if (!strcmp(line, "hub")) {
        if (strncmp(arg, "https://", 8) && strncmp(arg, "http://", 7)) { reply("ERR hub <http(s) base url>"); return; }
        strlcpy(s_hub, arg, sizeof(s_hub));
        while (s_hub[0] && s_hub[strlen(s_hub) - 1] == '/') s_hub[strlen(s_hub) - 1] = 0;
        store_blob("hub", s_hub, strlen(s_hub) + 1); s_want_check = 1;
        reply("OK hub set");
    } else if (!strcmp(line, "check")) {
        char sh[129] = "-";
        hub_check(); draw();
        if (s_hub_state == HUB_OK) hex(s_pending_sha, 64, sh);
        reply("%s check state=%d pending=%d sha=%s err=%s", s_hub_state == HUB_OK ? "OK" : "ERR",
              (int)s_hub_state, s_n_changes, sh, s_hub_err[0] ? s_hub_err : "-");
    } else if (!strcmp(line, "sign")) {
        cmd_sign(arg);
    } else if (!strcmp(line, "pin")) {
        cmd_pin(arg);
    } else if (!strcmp(line, "export")) {
        cmd_export(arg);
    } else if (!strcmp(line, "import")) {
        cmd_import(arg);
    } else if (!strcmp(line, "flip")) {
        s_flip = !s_flip; store_blob("flip", &s_flip, sizeof(s_flip)); lcd_set_flip(s_flip); draw();
        reply("OK flip=%d", s_flip);
    } else if (line[0]) {
        reply("ERR unknown command");
    }
}

static void serial_task(void *arg)
{
    static char line[512];
    int n = 0;
    uint8_t ch;
    for (;;) {
        if (usb_serial_jtag_read_bytes(&ch, 1, pdMS_TO_TICKS(1000)) != 1) continue;
        if (ch == '\r') continue;
        if (ch == '\n') { line[n] = 0; n = 0; handle(line); continue; }
        if (n < (int)sizeof(line) - 1) line[n++] = (char)ch;
    }
}

void app_main(void)
{
    esp_err_t e = nvs_flash_init();
    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    s_ev = xEventGroupCreate();
    s_lock = xSemaphoreCreateMutex();
    load_blob("flip", &s_flip, sizeof(s_flip));
    lcd_init(s_flip);
    lcd_text(3, 30, "start...", C_DIM, 1);
    lcd_flush();
    gpio_config_t io = {.pin_bit_mask = 1ULL << PIN_BTN, .mode = GPIO_MODE_INPUT, .pull_up_en = 1};
    gpio_config(&io);
    usb_serial_jtag_driver_config_t uc = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    uc.tx_buffer_size = 1024;            /* a reply larger than the TX buffer is dropped whole */
    uc.rx_buffer_size = 1024;
    usb_serial_jtag_driver_install(&uc);
    wifi_init();                         /* radio on first: real entropy for key generation */
    key_init();
    s_nnets = (int)(load_blob("wifi", s_nets, sizeof(s_nets)) / sizeof(net_t));
    load_blob("hub", s_hub, sizeof(s_hub) - 1);
    { char nm[17] = {0}; if (load_blob("name", nm, 16) > 1) strlcpy(s_name, nm, sizeof(s_name)); }
    xTaskCreate(serial_task, "serial", 12288, NULL, 5, NULL);

    int64_t last = 0;
    for (;;) {
        int64_t now = esp_timer_get_time() / 1000000;
        if (!s_prompt && (s_want_check || now - last >= CHECK_PERIOD_S)) {
            s_want_check = 0;
            last = now;
            hub_check();
            draw();
        }
        vTaskDelay(pdMS_TO_TICKS(250));
    }
}

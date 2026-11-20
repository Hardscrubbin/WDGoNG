/* ST7735S 0.96" 80x160 on the LilyGO T-Dongle-C5, driven in landscape (160x80).
 * Pins and quirks per the board notes: hardware SPI (SCK=6, MOSI=2), CS=10, DC=3,
 * RST=1, backlight on GPIO0 ACTIVE LOW (plain GPIO - PWM there breaks USB),
 * GREENTAB160x80 window offset, BGR + inversion. */
#include <string.h>
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "font.h"
#include "lcd.h"

#define PIN_SCK 6
#define PIN_MOSI 2
#define PIN_CS 10
#define PIN_DC 3
#define PIN_RST 1
#define PIN_BL 0

static spi_device_handle_t s_spi;
static uint16_t s_fb[LCD_W * LCD_H];        /* RGB565, already byte-swapped for the wire */
static int s_flip;

static void tx(const uint8_t *d, size_t n, int dc)
{
    spi_transaction_t t = {.length = n * 8, .tx_buffer = d};
    gpio_set_level(PIN_DC, dc);
    spi_device_polling_transmit(s_spi, &t);
}

static void cmd(uint8_t c, const uint8_t *args, size_t n)
{
    tx(&c, 1, 0);
    if (n) {
        tx(args, n, 1);
    }
}

#define CMD(c, ...) do { const uint8_t a_[] = {__VA_ARGS__}; cmd((c), a_, sizeof(a_)); } while (0)

static void set_rotation(void)
{
    /* MV + (MY or MX) + BGR: landscape, either way up */
    CMD(0x36, s_flip ? 0x68 : 0xA8);
}

void lcd_init(int flip)
{
    s_flip = flip;
    gpio_config_t io = {.pin_bit_mask = (1ULL << PIN_DC) | (1ULL << PIN_RST) | (1ULL << PIN_BL),
                        .mode = GPIO_MODE_OUTPUT};
    gpio_config(&io);
    gpio_set_level(PIN_BL, 1);              /* backlight off while we set up */
    spi_bus_config_t bus = {.mosi_io_num = PIN_MOSI, .miso_io_num = -1, .sclk_io_num = PIN_SCK,
                            .quadwp_io_num = -1, .quadhd_io_num = -1,
                            .max_transfer_sz = LCD_W * LCD_H * 2 + 8};
    spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO);
    spi_device_interface_config_t dev = {.clock_speed_hz = 20 * 1000 * 1000, .mode = 0,
                                         .spics_io_num = PIN_CS, .queue_size = 1};
    spi_bus_add_device(SPI2_HOST, &dev, &s_spi);

    gpio_set_level(PIN_RST, 0); vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(PIN_RST, 1); vTaskDelay(pdMS_TO_TICKS(120));
    CMD(0x01); vTaskDelay(pdMS_TO_TICKS(150));              /* SWRESET */
    CMD(0x11); vTaskDelay(pdMS_TO_TICKS(120));              /* SLPOUT */
    CMD(0xB1, 0x05, 0x3A, 0x3A);                            /* FRMCTR1 */
    CMD(0xB2, 0x05, 0x3A, 0x3A);
    CMD(0xB3, 0x05, 0x3A, 0x3A, 0x05, 0x3A, 0x3A);
    CMD(0xB4, 0x03);                                        /* INVCTR */
    CMD(0xC0, 0x62, 0x02, 0x04);                            /* PWCTR1..5 */
    CMD(0xC1, 0xC0);
    CMD(0xC2, 0x0D, 0x00);
    CMD(0xC3, 0x8D, 0x6A);
    CMD(0xC4, 0x8D, 0xEE);
    CMD(0xC5, 0x0E);                                        /* VMCTR1 */
    CMD(0xE0, 0x10, 0x0E, 0x02, 0x03, 0x0E, 0x07, 0x02, 0x07, 0x0A, 0x12, 0x27, 0x37, 0x00, 0x0D, 0x0E, 0x10);
    CMD(0xE1, 0x10, 0x0E, 0x03, 0x03, 0x0F, 0x06, 0x02, 0x08, 0x0A, 0x13, 0x26, 0x36, 0x00, 0x0D, 0x0E, 0x10);
    CMD(0x3A, 0x05);                                        /* 16 bpp */
    set_rotation();
    CMD(0x21);                                              /* INVON */
    CMD(0x29);                                              /* DISPON */
    lcd_clear(0);
    lcd_flush();
    gpio_set_level(PIN_BL, 0);                              /* backlight on (active low) */
}

void lcd_set_flip(int flip)
{
    s_flip = flip;
    set_rotation();
    lcd_flush();
}

void lcd_clear(uint16_t c)
{
    uint16_t w = (uint16_t)((c >> 8) | (c << 8));
    for (int i = 0; i < LCD_W * LCD_H; i++) {
        s_fb[i] = w;
    }
}

void lcd_rect(int x, int y, int w, int h, uint16_t c)
{
    uint16_t v = (uint16_t)((c >> 8) | (c << 8));
    for (int yy = y; yy < y + h; yy++) {
        if (yy < 0 || yy >= LCD_H) continue;
        for (int xx = x; xx < x + w; xx++) {
            if (xx >= 0 && xx < LCD_W) s_fb[yy * LCD_W + xx] = v;
        }
    }
}

void lcd_text(int x, int y, const char *s, uint16_t c, int scale)
{
    for (; *s; s++, x += FONT_W * scale) {
        unsigned ch = (unsigned char)*s;
        if (ch < 32 || ch > 126) ch = '?';
        const uint8_t *g = font6x10[ch - 32];
        for (int r = 0; r < FONT_H; r++) {
            for (int col = 0; col < FONT_W; col++) {
                if (g[r] & (0x80 >> col)) {
                    lcd_rect(x + col * scale, y + r * scale, scale, scale, c);
                }
            }
        }
    }
}

void lcd_flush(void)
{
    /* GREENTAB160x80 offsets, swapped for landscape */
    const int xo = 1, yo = 26;
    CMD(0x2A, 0, xo, 0, xo + LCD_W - 1);
    CMD(0x2B, 0, yo, 0, yo + LCD_H - 1);
    CMD(0x2C);
    tx((const uint8_t *)s_fb, sizeof(s_fb), 1);
}

#pragma once
#include <stdint.h>

#define LCD_W 160
#define LCD_H 80

#define C_BLACK 0x0000
#define C_WHITE 0xFFFF
#define C_DIM 0x7BEF
#define C_CYAN 0x07FF
#define C_YELLOW 0xFFE0
#define C_GREEN 0x07E0
#define C_RED 0xF800
#define C_MANGO 0xFD45      /* header colour; black text on it */

void lcd_init(int flip);
void lcd_set_flip(int flip);
void lcd_clear(uint16_t c);
void lcd_rect(int x, int y, int w, int h, uint16_t c);
void lcd_text(int x, int y, const char *s, uint16_t c, int scale);
void lcd_flush(void);

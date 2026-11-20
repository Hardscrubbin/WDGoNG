idf_component_register(SRCS "main.c" "lcd.c" INCLUDE_DIRS "."
    REQUIRES nvs_flash esp_wifi esp_netif esp_event esp_http_client esp-tls mbedtls json
             esp_driver_gpio esp_driver_spi esp_driver_usb_serial_jtag esp_timer)

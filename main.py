from time import localtime, sleep, strftime
import board
import digitalio
import adafruit_matrixkeypad

import wait_timesync
import lcd


cols = [digitalio.DigitalInOut(x) for x in (board.D10, board.D9, board.D11, board.D5)] # cols in order (left to right)
rows = [digitalio.DigitalInOut(x) for x in (board.D4, board.D17, board.D27, board.D22)] # rows in order (up to down)

keys = ((1, 2, 3, "A"), (4, 5, 6, "B"), (7, 8, 9, "C"), ("*", 0, "#", "D"))

keypad = adafruit_matrixkeypad.Matrix_Keypad(rows, cols, keys)

lcd.setup()
lcd.lcd_string("testing internet", 1)
lcd.lcd_string("connection", 2)

if wait_timesync.wait_for_sync():
    lcd.lcd_clear()
    lcd.lcd_string("time synced", 1)
    lcd.lcd_string(strftime("%d.%m.%Y %H:%M", localtime()), 2)

else:
    lcd.lcd_clear()
    lcd.lcd_string("time sync failed", 1)
    lcd.lcd_string("check connection", 2)



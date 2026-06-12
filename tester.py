from time import sleep
import board
import digitalio
import adafruit_matrixkeypad

import lcd


cols = [digitalio.DigitalInOut(x) for x in (board.D10, board.D9, board.D11, board.D5)] # cols in order (left to right)
rows = [digitalio.DigitalInOut(x) for x in (board.D4, board.D17, board.D27, board.D22)] # rows in order (up to down)

keys = ((1, 2, 3, "A"), (4, 5, 6, "B"), (7, 8, 9, "C"), ("*", 0, "#", "D"))

keypad = adafruit_matrixkeypad.Matrix_Keypad(rows, cols, keys)

lcd.setup()
text=""

while True:
    keys = keypad.pressed_keys
    if len(keys) > 1:
        continue #todo: error

    if keys:
        print("Pressed: ", keys)

        text += str(keys[0])
        if len(text) > 32:
            text = text[1:] # replace previous char and shift left
        if len(text) > 16:
            lcd.lcd_string(text[16:], 2)
        lcd.lcd_string(text, 1)
        
        sleep(0.1)
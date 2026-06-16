from time import localtime, sleep, strftime
import board
import digitalio
import adafruit_matrixkeypad

import wait_timesync
import lcd
import db


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

sleep(1)

db.init_db()
code = ""
previouscode=""
previoustime=""

while True:
    keys = keypad.pressed_keys
    if len(keys) > 1:
        continue #todo: handle error
    if keys:
        print("Pressed: ", keys)

        if str(keys[0]) == "#":
            lcd.lcd_clear()
            lcd.lcd_string("Processing code", 1)
            result = db.handle_code(code)
            if result["code"] == 0:
                lcd.lcd_clear()
                lcd.lcd_string(f"Hello, {result['name']}!", 1)  #TODO funny messages
                lcd.lcd_string(f"Checked {result['inout']}", 2)
                sleep(1)
            elif result["code"] == 1:
                lcd.lcd_clear()
                lcd.lcd_string(result["msg"], 1)
                sleep(1)
            else:
                lcd.lcd_clear()
                lcd.lcd_string("unknown error", 1)
        elif str(keys[0]) == "*":
            code=code[:-1]

        else:
            if len(code) < 10:
                code += str(keys[0])
    
    time= strftime("%d.%m.%Y %H:%M", localtime())
    if code == "" and (code != previouscode or previoustime != time):
        lcd.lcd_clear()
        lcd.lcd_string(time, 1)
        lcd.lcd_string("Enter code", 2)
        print("printed time: " + time)
    else:
        lcd.lcd_string(code, 1)
        sleep(0.1)
    previouscode=code
    previoustime=time
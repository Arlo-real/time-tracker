import RPi.GPIO as GPIO
from time import sleep

# Define GPIO to LCD mapping
LCD_RS = 7
LCD_E  = 8
LCD_D4 = 25
LCD_D5 = 24
LCD_D6 = 23
LCD_D7 = 18
 
# Define some device constants
LCD_WIDTH = 16    # Maximum characters per line
LCD_CHR = True
LCD_CMD = False

LCD_LINE_1 = 0x80 # LCD RAM address for the 1st line
LCD_LINE_2 = 0xC0 # LCD RAM address for the 2nd line
 
# Timing constants
E_PULSE = 0.002
E_DELAY = 0.002


def main():
  
  setup()
 
  while True:
 
    lcd_string("  Lcd working  ",1)
    lcd_string("    normaly    ",2)
    
    sleep(3)
    
    lcd_string("lcd.py ran, not",1)
    lcd_string("the main script",2)

 
    sleep(3)

def setup():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)       # Use BCM GPIO numbers
    GPIO.setup(LCD_E, GPIO.OUT)  # E
    GPIO.setup(LCD_RS, GPIO.OUT) # RS
    GPIO.setup(LCD_D4, GPIO.OUT) # DB4
    GPIO.setup(LCD_D5, GPIO.OUT) # DB5
    GPIO.setup(LCD_D6, GPIO.OUT) # DB6
    GPIO.setup(LCD_D7, GPIO.OUT) # DB7
    # Initialise display
    lcd_init()


def lcd_send_nibble(nibble):
    GPIO.output(LCD_RS, False)
    GPIO.output(LCD_D4, bool(nibble & 0x01))
    GPIO.output(LCD_D5, bool(nibble & 0x02))
    GPIO.output(LCD_D6, bool(nibble & 0x04))
    GPIO.output(LCD_D7, bool(nibble & 0x08))
    lcd_toggle_enable()

def lcd_init():
    sleep(0.05)

    lcd_send_nibble(0x03); sleep(0.005)
    lcd_send_nibble(0x03); sleep(0.001)
    lcd_send_nibble(0x03); sleep(0.001)
    lcd_send_nibble(0x02); sleep(0.001)

    lcd_display(0x28, LCD_CMD)
    lcd_display(0x0C, LCD_CMD)
    lcd_display(0x06, LCD_CMD)
    lcd_display(0x01, LCD_CMD)
    sleep(0.005)
    

def lcd_display(bits, mode):
  # Send byte to data pins
  # bits = data
  # mode = True  for character
  #        False for command
 
  GPIO.output(LCD_RS, mode) # RS
 
  # High bits
  GPIO.output(LCD_D4, False)
  GPIO.output(LCD_D5, False)
  GPIO.output(LCD_D6, False)
  GPIO.output(LCD_D7, False)
  if bits&0x10==0x10:
    GPIO.output(LCD_D4, True)
  if bits&0x20==0x20:
    GPIO.output(LCD_D5, True)
  if bits&0x40==0x40:
    GPIO.output(LCD_D6, True)
  if bits&0x80==0x80:
    GPIO.output(LCD_D7, True)
 
  # Toggle 'Enable' pin
  lcd_toggle_enable()
 
  # Low bits
  GPIO.output(LCD_D4, False)
  GPIO.output(LCD_D5, False)
  GPIO.output(LCD_D6, False)
  GPIO.output(LCD_D7, False)
  if bits&0x01==0x01:
    GPIO.output(LCD_D4, True)
  if bits&0x02==0x02:
    GPIO.output(LCD_D5, True)
  if bits&0x04==0x04:
    GPIO.output(LCD_D6, True)
  if bits&0x08==0x08:
    GPIO.output(LCD_D7, True)
 
  # Toggle 'Enable' pin
  lcd_toggle_enable()

def lcd_toggle_enable():
  # Toggle enable
  sleep(E_DELAY)
  GPIO.output(LCD_E, True)
  sleep(E_PULSE)
  GPIO.output(LCD_E, False)
  sleep(E_DELAY)
 
def lcd_string(message,line=1):
  # Send string to display
    if line==1:
        line = LCD_LINE_1
    elif line==2:
        line = LCD_LINE_2
    else:
        raise ValueError("line must be 1 or 2")
    message = message.ljust(LCD_WIDTH," ")
 
    lcd_display(line, LCD_CMD)
 
    for i in range(LCD_WIDTH):
        lcd_display(ord(message[i]),LCD_CHR)
def lcd_clear():
    lcd_display(0x01, LCD_CMD)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        lcd_clear()
        GPIO.cleanup()
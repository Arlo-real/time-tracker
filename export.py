import db
from time import localtime, strftime
db.init_db()

print ("1) export everything")
print ("2) export only current day")
print ("3) export only current month")
print ("4) export only current year")
ans=input("")
month = strftime("%Y-%m", localtime())
year = strftime("%Y", localtime())
if ans == "1":
    print("Sepatate into:")
    print("1) dayly files")
    print("2) monthly files")
    print("3) one file")
    ans2=input("")
    if ans2 == "1":
    elif ans2 == "2":
    elif ans2 == "3":
        db.export_csv("1970-01-01", "2100-01-01", f"full history the {strftime("%Y.%m.%d", localtime())} at {strftime('%H:%M', localtime())}.csv")
elif ans == "2":
    today = strftime("%Y-%m-%d", localtime())
    db.export_csv(today, today, f"export the {today} at {strftime('%H:%M', localtime())}.csv")
elif ans == "3":    
    db.export_csv(f"{month}-01", f"{month}-31", f"export month {month} the {strftime('%Y.%m.%d', localtime())} at {strftime('%H:%M', localtime())}.csv")
elif ans == "4":
    db.export_csv(f"{year}-01-01", f"{year}-12-31", f"export year {year} the {strftime('%Y.%m.%d', localtime())} at {strftime('%H:%M', localtime())}.csv")
else:    print("invalid input")

db.export_csv()
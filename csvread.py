import csv

with open("employees.csv", "r", newline="", encoding="utf-8") as f:
    reader = csv.reader(f)
    headers = next(reader)  # Read the header row
    print(f"Columns: {headers}")
    
    for row in reader:
        name, age, dept, salary = row
        print(f"{name} works in {dept} and is {age} years old.")
import csv

# Writing to a CSV file
data = [
    ["Name", "Age", "Department", "Salary"],
    ["Alice", 32, "Engineering", 72000],
    ["Bob", 45, "Marketing", 91000],
    ["Carol", 28, "Engineering", 58000],
    ["David", 38, "Sales", 68000],
]

with open("employees.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerows(data)

print("CSV file written successfully.")
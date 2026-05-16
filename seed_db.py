"""
seed_db.py — Database Seeder (Self-Clearing)
===========================================
Wipes old tables and copies JSON data from mock_data.py into Postgres.
"""
from datetime import date, timedelta
from sqlalchemy import text

from db import session_scope
from models import Department, Person, Assignment, Leave, Bounty, SkillProfile
from mock_data import MOCK_ERP_DATA

def seed_database():
    print("Starting database seed...")
    with session_scope() as db:
        
        # 1. Clear out any leftover data from previous attempts
        print("Clearing old data from tables...")
        db.execute(text("TRUNCATE departments, people, assignments, leaves, bounties, skill_profiles CASCADE;"))
        db.commit()

        # 2. Create unique departments from the mock employees
        print("Seeding fresh departments...")
        dept_names = set(emp.get("department", "Engineering") for emp in MOCK_ERP_DATA["employees"])
        dept_map = {}
        for idx, name in enumerate(dept_names):
            dept = Department(erp_department_id=f"DEPT-{idx+100}", name=name)
            db.add(dept)
            db.flush() # Get the database ID before committing
            dept_map[name] = dept.id

        # 3. Add People & Skills
        print("Seeding employee records...")
        for emp in MOCK_ERP_DATA["employees"]:
            person = Person(
                employee_id=emp["id"],
                name=emp["name"],
                department_id=dept_map[emp.get("department", "Engineering")],
                role=emp["role"],
                capacity_hours_per_week=emp.get("capacity_hours_per_week", 40.0),
                hourly_rate_usd=emp.get("hourly_rate_usd", 100.0),
                current_stage="intern_hybrid", # Defaulting to test STROMA gates
                join_date=date.today() - timedelta(days=150),
                stage_start_date=date.today() - timedelta(days=50),
                active=True
            )
            db.add(person)
            
            skills = SkillProfile(employee_id=emp["id"], skills=emp.get("skills", []))
            db.add(skills)

        # 4. Add Assignments
        for asg in MOCK_ERP_DATA.get("assignments", []):
            db.add(Assignment(**asg))

        # 5. Add Leaves
        for lv in MOCK_ERP_DATA.get("leaves", []):
            db.add(Leave(**lv))

        # 6. Add Bounties
        for bnt in MOCK_ERP_DATA.get("bounties", []):
            db.add(Bounty(**bnt))

    print("✅ Database successfully seeded!")

if __name__ == "__main__":
    seed_database()
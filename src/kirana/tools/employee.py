"""Employee & Store Security Management Tools.

Provides tools for:
  - Listing registered employees and cashiers
  - Adding new employees with auto-generated EMP-xxx codes
  - Deactivating employees with 2-phase confirmation and owner PIN authorization
  - Updating or setting the store owner security PIN / password
"""

from __future__ import annotations

from ._helpers import arg, err, guard, ok
from .base import tool
from .context import current_chat, get_repo
from ..security.password import check_pin_rate_limit, record_failed_attempt, reset_rate_limit


@tool(
    "list_employees",
    "List all store staff and cashiers registered for this store, including their unique Employee IDs (e.g. EMP-001), roles, and active status.",
    {
        "type": "object",
        "properties": {
            "include_inactive": {
                "type": "boolean",
                "description": "Whether to include deactivated/former employees (default false)",
            },
        },
    },
)
@guard
async def list_employees(args: dict) -> dict:
    repo = get_repo()
    include_inactive = bool(arg(args, "include_inactive", False))
    employees = repo.list_employees(include_inactive=include_inactive)
    if not employees:
        return ok("No employees registered for this store yet.", count=0, employees=[])

    lines = ["👥 **Registered Store Staff & Cashiers:**"]
    data = []
    for emp in employees:
        status_tag = "" if emp.is_active else " *(Inactive / Deactivated)*"
        lines.append(f"• **{emp.emp_code}**: {emp.name} ({emp.role.capitalize()}){status_tag}")
        data.append({
            "id": emp.id,
            "emp_code": emp.emp_code,
            "name": emp.name,
            "role": emp.role,
            "is_active": emp.is_active,
        })
    return ok("\n".join(lines), count=len(employees), employees=data)


@tool(
    "add_employee",
    "Register a new store employee or cashier. Generates a unique sequential Employee ID (e.g. EMP-001, EMP-002) and makes them available for billing shifts.",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Full or first name of the employee / cashier (e.g. 'Ramesh', 'Priya')",
            },
            "role": {
                "type": "string",
                "description": "Role of the employee (default 'cashier', or 'manager', 'sales')",
            },
            "phone": {
                "type": "string",
                "description": "Optional contact phone number for the employee",
            },
        },
        "required": ["name"],
    },
)
@guard
async def add_employee(args: dict) -> dict:
    repo = get_repo()
    name = str(arg(args, "name", "")).strip()
    role = str(arg(args, "role", "cashier")).strip()
    phone = arg(args, "phone")

    if not name or len(name) < 2:
        return err("Employee name must be at least 2 characters long.", "invalid_name")

    emp = repo.add_employee(name=name, role=role, phone=phone)
    return ok(
        f"✅ Employee **{emp.name}** registered successfully with Employee ID **{emp.emp_code}** ({emp.role.capitalize()}).",
        id=emp.id,
        emp_code=emp.emp_code,
        name=emp.name,
        role=emp.role,
    )


@tool(
    "remove_employee",
    "Deactivate or remove a store employee. Strictly requires confirmation (confirm=true) and the Store Owner Security PIN / Password (password=...). Deactivation safely retains historical billing attribution for GST and tax audits.",
    {
        "type": "object",
        "properties": {
            "code_or_name": {
                "type": "string",
                "description": "Employee ID (e.g. 'EMP-001') or name (e.g. 'Ramesh') of the employee to deactivate",
            },
            "password": {
                "type": "string",
                "description": "Store owner security PIN / password for authorization",
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true to confirm removal after warning the store owner",
            },
        },
        "required": ["code_or_name"],
    },
)
@guard
async def remove_employee(args: dict) -> dict:
    repo = get_repo()
    chat_id = current_chat.get() or 0
    code_or_name = str(arg(args, "code_or_name", "")).strip()
    password = arg(args, "password")
    confirm = bool(arg(args, "confirm", False))

    emp = repo.get_employee(code_or_name)
    if not emp:
        return err(f"No employee found matching '{code_or_name}'.", "employee_not_found")

    if not emp.is_active:
        return ok(f"Employee {emp.display()} is already inactive.", emp_code=emp.emp_code, is_active=False)

    has_owner_pw = repo.has_owner_password()
    if not confirm or (has_owner_pw and not password):
        return ok(
            f"⚠️ **CONFIRMATION & AUTHORIZATION REQUIRED**\n\n"
            f"Are you sure you want to remove employee **{emp.display()}**?\n"
            f"• This will deactivate their profile and remove them from active store cashiers.\n"
            f"• Historical bills and sales records created by them will remain safely preserved for GST and audit compliance.\n\n"
            f"👉 To proceed, please confirm and provide your Store Owner Security PIN / Password.",
            requires_confirmation=True,
            requires_password=has_owner_pw,
            emp_code=emp.emp_code,
            name=emp.name,
        )

    if has_owner_pw:
        rl = check_pin_rate_limit(chat_id)
        if rl.is_locked:
            mins = int(rl.lockout_remaining_seconds // 60) + 1
            return err(f"Too many failed security attempts. Rate-limited for {mins} minutes.", "locked_out")

        if not repo.verify_owner_password(password):
            rl = record_failed_attempt(chat_id)
            if rl.is_locked:
                mins = int(rl.lockout_remaining_seconds // 60) + 1
                return err(f"❌ Invalid password. Maximum attempts exceeded. Locked for {mins} minutes.", "locked_out")
            return err("❌ Invalid store owner security password/PIN. Deactivation rejected.", "invalid_password")

        reset_rate_limit(chat_id)

    repo.remove_employee(emp.id)
    return ok(
        f"✅ Employee **{emp.display()}** has been successfully deactivated. "
        f"They can no longer be selected for new billing shifts. Historical sales records remain intact.",
        emp_code=emp.emp_code,
        name=emp.name,
        is_active=False,
    )


@tool(
    "change_owner_password",
    "Change or set the Store Owner Security PIN / Password. Requires old_password if one is already configured.",
    {
        "type": "object",
        "properties": {
            "new_password": {
                "type": "string",
                "description": "New security PIN or password (min 4 characters)",
            },
            "old_password": {
                "type": "string",
                "description": "Current security PIN or password (required if previously set)",
            },
        },
        "required": ["new_password"],
    },
)
@guard
async def change_owner_password(args: dict) -> dict:
    repo = get_repo()
    chat_id = current_chat.get() or 0
    new_pw = str(arg(args, "new_password", "")).strip()
    old_pw = arg(args, "old_password")

    if len(new_pw) < 4:
        return err("New password/PIN must be at least 4 characters long.", "password_too_short")

    if repo.has_owner_password():
        rl = check_pin_rate_limit(chat_id)
        if rl.is_locked:
            mins = int(rl.lockout_remaining_seconds // 60) + 1
            return err(f"Too many failed security attempts. Rate-limited for {mins} minutes.", "locked_out")

        if not old_pw or not repo.verify_owner_password(old_pw):
            rl = record_failed_attempt(chat_id)
            if rl.is_locked:
                mins = int(rl.lockout_remaining_seconds // 60) + 1
                return err(f"❌ Invalid password. Maximum attempts exceeded. Locked for {mins} minutes.", "locked_out")
            return err("❌ Invalid current store owner password/PIN.", "invalid_password")

        reset_rate_limit(chat_id)

    repo.set_owner_password(new_pw)
    return ok("✅ Store owner security password/PIN updated successfully.")


EMPLOYEE_TOOLS = [
    list_employees,
    add_employee,
    remove_employee,
    change_owner_password,
]

# Class Co-Owners Feature

## Overview
The co-owners feature allows the primary teacher of a class to grant other teachers/admins the ability to co-manage the class. Co-owners have full management permissions equivalent to the primary teacher, except they cannot delete the co-owner relationship itself (only the primary teacher can remove co-owners).

## Database Schema

### Association Table: `class_co_owners`
```python
class_co_owners = db.Table('class_co_owners',
    db.Column('class_id', db.Integer, db.ForeignKey('class.id'), primary_key=True),
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True)
)
```

### Relationships
- **User model**: `co_owned_classes = db.relationship('Class', secondary=class_co_owners, backref='co_owners')`
- **Class model**: `co_owners` (backref from User model with `lazy='dynamic'`)

### Permission Check Method
```python
def is_owner(self, user):
    """Check if user is the primary teacher or a co-owner."""
    if self.teacher_id == user.id:
        return True
    return user in self.co_owners.all()
```

## API Endpoints

### GET `/api/classes/<class_id>/co-owners`
Returns list of co-owners and primary teacher information.

**Response:**
```json
{
  "ok": true,
  "primary_teacher": {
    "id": 1,
    "username": "teacher1",
    "role": "teacher"
  },
  "co_owners": [
    {
      "id": 2,
      "username": "teacher2",
      "role": "teacher"
    }
  ]
}
```

### POST `/api/classes/<class_id>/co-owners`
Add a user as co-owner (primary teacher or admin only).

**Request:**
```json
{
  "user_id": 2
}
```

**Response:**
```json
{
  "ok": true,
  "message": "Co-owner added successfully"
}
```

### DELETE `/api/classes/<class_id>/co-owners/<user_id>`
Remove a co-owner (primary teacher or admin only).

**Response:**
```json
{
  "ok": true,
  "message": "Co-owner removed successfully"
}
```

### GET `/api/users/teachers`
List all users with teacher or adminer roles (for co-owner selection).

**Response:**
```json
{
  "ok": true,
  "teachers": [
    {
      "id": 1,
      "username": "teacher1",
      "role": "teacher"
    },
    {
      "id": 2,
      "username": "admin1",
      "role": "adminer"
    }
  ]
}
```

## UI Components

### Class Detail Page - Co-Owners Section
Located in `app/templates/classes/detail.html`:

1. **Sidebar Navigation**: "Co-Owners" link with person-badge icon
2. **Content Section**: 
   - Displays primary teacher with star icon
   - Lists all co-owners with remove buttons
   - "Add Co-Owner" button to add new co-owners

### Co-Owner Modal
Modal dialog for selecting a teacher/admin to add as co-owner:
- Dropdown populated with all available teachers/admins
- Filters out current co-owners and primary teacher
- "Add Co-Owner" button to confirm

### JavaScript Functions
- `loadCoOwners()`: Fetches and displays current co-owners
- `loadAvailableTeachers()`: Fetches list of teachers/admins for dropdown
- `showAddCoOwnerModal()`: Opens modal with teacher selection
- `addCoOwner()`: Sends POST request to add selected user
- `removeCoOwner(userId, username)`: Sends DELETE request after confirmation

## Permission Updates

All permission checks in `app/routes/api/classes.py` have been updated from:
```python
if not user.is_adminer and class_.teacher_id != user.id:
    return jsonify({"ok": False, "error": "Access denied"}), 403
```

To:
```python
if not user.is_adminer and not class_.is_owner(user):
    return jsonify({"ok": False, "error": "Access denied"}), 403
```

This change affects 16 API endpoints:
- GET/PUT/DELETE `/api/classes/<id>`
- POST/DELETE `/api/classes/<id>/invite`
- GET `/api/classes/<id>/vms`
- POST `/api/classes/<id>/pool`
- POST `/api/classes/<id>/commit-teacher-vm`
- POST `/api/classes/<id>/replicate-template`
- POST `/api/classes/<id>/assign/<assignment_id>`
- DELETE `/api/classes/<id>/unassign/<assignment_id>`
- DELETE `/api/classes/<id>/remove-vm/<assignment_id>`
- POST `/api/classes/<id>/auto-assign`
- POST `/api/classes/<id>/create-baseline-snapshot`
- POST `/api/classes/<id>/deploy-vms` (Terraform)
- POST `/api/classes/<id>/destroy-vms` (Terraform)

## Usage Workflow

1. **Primary teacher creates class**
   - Teacher creates class as normal via `/classes/create`
   - Only the teacher can manage class initially

2. **Add co-owner**
   - Teacher navigates to class detail page
   - Clicks "Co-Owners" tab in sidebar
   - Clicks "Add Co-Owner" button
   - Selects teacher/admin from dropdown
   - Clicks "Add Co-Owner" to confirm

3. **Co-owner access**
   - Co-owner can now see the class in their class list
   - Co-owner has full management permissions:
     - Add/remove VMs
     - Generate/invalidate invite links
     - Assign/unassign students
     - Create snapshots
     - Deploy/destroy VMs via Terraform
   - Co-owner CANNOT:
     - Delete the class
     - Remove other co-owners
     - Transfer primary ownership

4. **Remove co-owner**
   - Primary teacher (or admin) clicks "Remove" next to co-owner
   - Confirms deletion
   - Co-owner immediately loses access to class

## Migration from Existing Classes

No database migration required - the `class_co_owners` table is created automatically via `db.create_all()` on first run. Existing classes will have empty co-owner lists until teachers manually add co-owners.

## Security Considerations

- **Primary teacher privileges**: Only the primary teacher (or admin) can add/remove co-owners
- **Admin override**: Admins can always manage any class regardless of owner/co-owner status
- **Co-owner equality**: All co-owners have equal permissions (cannot remove each other)
- **Cascading deletes**: When a class is deleted, co-owner relationships are automatically removed
- **User deletion**: If a co-owner's user account is deleted, their co-owner relationships are removed via foreign key cascade

## Testing Checklist

- [ ] Create class as Teacher A
- [ ] Add Teacher B as co-owner
- [ ] Verify Teacher B can see class in their list
- [ ] Verify Teacher B can manage VMs (add/remove/assign)
- [ ] Verify Teacher B can generate invite links
- [ ] Verify Teacher B cannot delete the class
- [ ] Verify Teacher B cannot remove Teacher A's co-owner relationships
- [ ] Remove Teacher B as co-owner
- [ ] Verify Teacher B no longer sees the class
- [ ] Verify primary teacher icon appears correctly
- [ ] Verify co-owner list updates in real-time after add/remove

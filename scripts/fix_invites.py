#!/usr/bin/env python3
"""
Fix broken class invites - diagnose and repair invite tokens.

Run this script to fix classes that show "No Invite" in the UI
despite having invite data in the database.
"""

from app import create_app
from app.models import db, Class
from datetime import datetime

def diagnose_and_fix_invites():
    app = create_app()
    with app.app_context():
        print("=" * 70)
        print("CLASS INVITE DIAGNOSTICS")
        print("=" * 70)
        
        all_classes = Class.query.all()
        print(f"\nFound {len(all_classes)} classes\n")
        
        broken_count = 0
        fixed_count = 0
        
        for class_ in all_classes:
            print(f"\nClass #{class_.id}: {class_.name}")
            print(f"  Teacher ID: {class_.teacher_id}")
            print(f"  Join Token: {class_.join_token}")
            print(f"  Token Expires At: {class_.token_expires_at}")
            print(f"  Token Never Expires: {class_.token_never_expires}")
            print(f"  Token Valid: {class_.is_token_valid()}")
            
            # Check for broken state: has token but invalid
            if class_.join_token and not class_.is_token_valid():
                broken_count += 1
                print("  ⚠️  BROKEN: Token exists but is_token_valid() returns False")
                
                # Check specific issues
                if not class_.token_never_expires and not class_.token_expires_at:
                    print("  ⚠️  Issue: Token has no expiration data")
                    print("  🔧  FIX: Setting token_never_expires=True")
                    class_.token_never_expires = True
                    fixed_count += 1
                
                elif class_.token_expires_at and datetime.utcnow() > class_.token_expires_at:
                    print(f"  ⚠️  Issue: Token expired on {class_.token_expires_at}")
                    print("  🔧  FIX: Regenerating token that never expires")
                    class_.generate_join_token(expires_in_days=0)
                    fixed_count += 1
            
            elif not class_.join_token:
                print("  ℹ️  No invite token (create one in the web UI)")
            else:
                print("  ✓  Token is valid")
        
        if fixed_count > 0:
            print("\n" + "=" * 70)
            print(f"FIXING {fixed_count} BROKEN INVITE(S)")
            print("=" * 70)
            try:
                db.session.commit()
                print("✓ Successfully fixed all broken invites!")
            except Exception as e:
                db.session.rollback()
                print(f"✗ ERROR: Failed to save changes: {e}")
        else:
            print("\n" + "=" * 70)
            print("No fixes needed - all invites are valid!")
            print("=" * 70)
        
        if broken_count == 0:
            print("\n✓ All classes with invites are working correctly.")
        else:
            print(f"\n⚠️  Found {broken_count} broken invite(s)")
            if fixed_count == broken_count:
                print("✓ All issues have been fixed!")
            else:
                print(f"⚠️  {broken_count - fixed_count} issue(s) need manual attention")

if __name__ == "__main__":
    diagnose_and_fix_invites()

"""
Signing in, from the desktop app and from the phone.

Credentials are checked against the users table (see services/users). The
signed token goes back ONLY as an HttpOnly, SameSite=Strict cookie (Secure when
the request came over HTTPS): the browser sends it on every call — <img> tags
and new tabs included — and no script on the page can read it, so an injected
script cannot lift a session and replay it elsewhere.

The one exception is a native client (the React Native app), which has no page
for a script to run in and keeps its token in the device's own storage. It asks
for the token in the reply by sending `X-Essa-Client: native`.
"""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..services import audit as audit_svc, permissions, users as users_svc

router = APIRouter(prefix="/api/auth", tags=["auth"])

# A month. Long enough that the phone on the dock is not asked to log in again
# mid-receipt, and irrelevant to revocation, which goes through the token seed.
COOKIE_MAX_AGE = 60 * 60 * 24 * 30


class LoginIn(BaseModel):
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


def _wants_token(request: Request) -> bool:
    """Whether the caller is a native app that must be handed the token."""
    return request.headers.get("x-essa-client", "").lower() == "native"


def _is_https(request: Request) -> bool:
    # Behind nginx the app itself is spoken to over plain HTTP; the proxy says
    # what the browser actually used.
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return (proto or request.url.scheme) == "https"


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    """HttpOnly: invisible to JavaScript. SameSite=Strict: never sent on a
    request another site started, which is what keeps a cookie-carried session
    safe from cross-site forgery. Secure whenever the page is on HTTPS — not
    unconditionally, because a warehouse laptop served over http on the LAN
    would then never get its cookie back."""
    response.set_cookie("essa_token", token, max_age=COOKIE_MAX_AGE, path="/",
                        httponly=True, secure=_is_https(request), samesite="strict")


def _session(user: User, token: str = "") -> dict:
    out = {"ok": True, "user": user.username,
            "role": user.role, "role_label": users_svc.ROLE_LABEL.get(user.role, user.role),
            "full_name": user.full_name or "",
            # By rank, never by name: `role == "superadmin"` read False for the
            # Super Boss, who can do everything a super admin can.
            "can": {"manage_users": users_svc.rank(user.role) >= users_svc.ROLE_RANK["superadmin"],
                    "admin": users_svc.rank(user.role) >= users_svc.ROLE_RANK["admin"],
                    "command": users_svc.rank(user.role) >= users_svc.ROLE_RANK["superadmin"],
                    "boss": users_svc.rank(user.role) >= users_svc.ROLE_RANK["superboss"]},
            # What this account may do screen by screen, so the menu can show the
            # screens it has rather than the screens its role has. The server
            # refuses either way — this is what stops the floor being offered
            # twelve buttons that answer "not for you".
            "permissions": permissions.normalise(user.permissions)}
    if token:
        out["token"] = token
    return out


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response,
          db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username.strip()).first()
    # One message for both "no such user" and "wrong password", so the form
    # cannot be used to find out which usernames exist.
    if not user or not users_svc.verify_password(body.password, user.password_hash):
        # On the trail all the same: a run of these against one name is the
        # exception the Command Center exists to show. The name is recorded as
        # typed; the password never is.
        audit_svc.record(db, username=(body.username or "").strip()[:64] or "?",
                         role=user.role if user else None,
                         full_name=user.full_name if user else None,
                         method="POST", path="/api/auth/login", screen="session",
                         action="signin", outcome="failed", status=401,
                         summary="failed to sign in — wrong username or password")
        raise HTTPException(401, "Invalid username or password")
    if not user.active:
        audit_svc.record(db, username=user.username, full_name=user.full_name,
                         role=user.role, method="POST", path="/api/auth/login",
                         screen="session", action="signin", outcome="refused",
                         status=403, summary="tried to sign in to a deactivated account")
        raise HTTPException(403, "This account has been deactivated")

    token = users_svc.mint_token(user)
    user.last_login_at = dt.datetime.utcnow()
    db.commit()
    audit_svc.record(db, username=user.username, full_name=user.full_name,
                     role=user.role, method="POST", path="/api/auth/login",
                     screen="session", action="signin", outcome="ok", status=200,
                     summary="signed in")
    set_session_cookie(request, response, token)
    return _session(user, token if _wants_token(request) else "")


@router.get("/verify")
def verify(request: Request, db: Session = Depends(get_db)):
    """What a refresh asks: is the session cookie still good, and as whom? A
    role changed by the super admin lands here, so a demoted user's next reload
    shows the smaller app rather than waiting for them to sign out. The token is
    never echoed back — a page script able to call this must not be able to
    read the session out of the answer."""
    from ..security import token_from
    user = users_svc.resolve_token(db, token_from(request))
    if not user:
        return {"ok": False, "user": None, "role": None}
    return _session(user)


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("essa_token", path="/", httponly=True, samesite="strict")
    return {"ok": True}


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    from ..security import token_from
    user = users_svc.resolve_token(db, token_from(request))
    if not user:
        raise HTTPException(401, "Sign in to continue")
    return users_svc.out(user)


@router.post("/change-password")
def change_password(body: ChangePasswordIn, request: Request, response: Response,
                    db: Session = Depends(get_db)):
    """Anyone may change their own password, whatever their role."""
    from ..security import token_from
    user = users_svc.resolve_token(db, token_from(request))
    if not user:
        raise HTTPException(401, "Sign in to continue")
    if not users_svc.verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    problem = users_svc.password_problem(body.new_password)
    if problem:
        raise HTTPException(400, problem)
    users_svc.set_password(db, user, body.new_password)
    audit_svc.record(db, username=user.username, full_name=user.full_name,
                     role=user.role, method="POST", path="/api/auth/change-password",
                     screen="session", action="password", outcome="ok", status=200,
                     summary="changed their own password")
    # The seed rotated, so the token that authorised this call is now dead —
    # hand back a fresh one rather than bouncing them to the login screen.
    token = users_svc.mint_token(user)
    set_session_cookie(request, response, token)
    return {"ok": True, **({"token": token} if _wants_token(request) else {})}

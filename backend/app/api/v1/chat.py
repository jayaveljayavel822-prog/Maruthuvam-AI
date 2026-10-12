from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.api.dependencies import get_current_user
from app.models.user import User
from app.models.chat import Conversation, ChatMessage, AIUsageLog
from app.models.health_log import HealthLog
from app.models.medical_record import MedicalRecord
from app.models.medication import MedicationReminder
from app.schemas.chat import (
    ConversationIn,
    ConversationOut,
    MessageIn,
    MessageOut,
    QuickAiTaskIn,
    QuickAiTaskOut,
)
from app.services.ai_service import ai_service

router = APIRouter(prefix="/chat", tags=["AI Copilot"])


@router.get("/conversations", response_model=List[ConversationOut])
def list_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List chat conversation sessions for the user."""
    return db.query(Conversation).filter(
        Conversation.user_id == current_user.id
    ).order_by(Conversation.updated_at.desc()).all()


@router.post("/conversations", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
def create_conversation(
    payload: ConversationIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start a new health consultation thread."""
    conv = Conversation(
        user_id=current_user.id,
        title=payload.title or "New Consultation",
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)

    # Add initial greeting message
    greeting = ChatMessage(
        conversation_id=conv.id,
        user_id=current_user.id,
        role="assistant",
        content="Hello! I am your Maruthuvam.AI Health Copilot. Tell me how you are feeling, or ask me about your wellness habits and medical records. I provide general guidance, not a medical diagnosis.",
        is_emergency=False,
        language=current_user.preferred_language,
    )
    db.add(greeting)
    db.commit()
    db.refresh(conv)
    return conv


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve conversation messages."""
    conv = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id,
    ).first()
    if not conv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return conv


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete conversation history."""
    conv = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id,
    ).first()
    if not conv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    db.delete(conv)
    db.commit()
    return None


@router.post("/conversations/{conversation_id}/messages", response_model=MessageOut)
async def send_message(
    conversation_id: str,
    payload: MessageIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Send user prompt, run deterministic emergency guardrails,
    query AI Copilot, and persist both user and assistant messages.
    """
    conv = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id,
    ).first()
    if not conv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")

    # 1. Save user message
    user_msg = ChatMessage(
        conversation_id=conv.id,
        user_id=current_user.id,
        role="user",
        content=payload.content,
        is_emergency=False,
        language=payload.language or current_user.preferred_language,
    )
    db.add(user_msg)
    db.flush()

    # 2. Build patient clinical context (wellness logs + active records)
    recent_logs = db.query(HealthLog).filter(
        HealthLog.user_id == current_user.id
    ).order_by(HealthLog.log_date.desc()).limit(7).all()
    
    recent_recs = db.query(MedicalRecord).filter(
        MedicalRecord.user_id == current_user.id
    ).order_by(MedicalRecord.record_date.desc()).limit(5).all()

    reminders = db.query(MedicationReminder).filter(
        MedicationReminder.user_id == current_user.id,
        MedicationReminder.is_active == True,
    ).all()

    system_ctx = (
        f"Patient: {current_user.username}. Age: {current_user.profile.age if current_user.profile else 'N/A'}.\n"
        f"Active Reminders: {', '.join(r.medicine_name for r in reminders) if reminders else 'None'}.\n"
        f"Recent Records: {'; '.join(r.title for r in recent_recs) if recent_recs else 'None'}.\n"
    )

    # 3. Retrieve conversation history
    past_msgs = db.query(ChatMessage).filter(
        ChatMessage.conversation_id == conv.id
    ).order_by(ChatMessage.created_at.asc()).limit(6).all()
    history = [{"role": m.role, "content": m.content} for m in past_msgs]

    # 4. Generate AI response
    ai_resp = await ai_service.generate_response(
        prompt=payload.content,
        system_context=system_ctx,
        language=payload.language or current_user.preferred_language,
        user_history=history,
    )

    # 5. Save assistant message
    asst_msg = ChatMessage(
        conversation_id=conv.id,
        user_id=current_user.id,
        role="assistant",
        content=ai_resp["text"],
        is_emergency=ai_resp.get("is_emergency", False),
        language=payload.language or current_user.preferred_language,
    )
    db.add(asst_msg)

    # 6. Usage log
    usage = AIUsageLog(
        user_id=current_user.id,
        provider=ai_resp.get("provider", "unknown"),
        action="chat_consultation",
        prompt_tokens=len(payload.content.split()),
        completion_tokens=len(ai_resp["text"].split()),
    )
    db.add(usage)

    db.commit()
    db.refresh(asst_msg)
    return asst_msg


@router.post("/quick-task", response_model=QuickAiTaskOut)
async def perform_quick_ai_task(
    payload: QuickAiTaskIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Execute dedicated AI actions:
    weekly summary, daily tip, cross insights, clinician summary, doctor questions.
    """
    lang = payload.language or current_user.preferred_language
    today_log = db.query(HealthLog).filter(
        HealthLog.user_id == current_user.id
    ).order_by(HealthLog.log_date.desc()).first()

    recent_logs = db.query(HealthLog).filter(
        HealthLog.user_id == current_user.id
    ).order_by(HealthLog.log_date.desc()).limit(7).all()

    records = db.query(MedicalRecord).filter(
        MedicalRecord.user_id == current_user.id
    ).all()

    if payload.task_type == "weekly_summary":
        prompt = (
            "You are a supportive wellness coach. Based on these 7 days of lifestyle metrics:\n"
            f"{[f'{l.log_date}: water {l.water_glasses}, sleep {l.sleep_hours}h, steps {l.steps}' for l in recent_logs]}\n"
            "Provide a 4-line summary: 1 Win, 1 Area for attention, 1 Actionable step for tomorrow."
        )
    elif payload.task_type == "daily_tip":
        prompt = (
            "You are an encouraging daily health coach. Provide ONE practical, safe, single-sentence health tip "
            f"for today regarding hydration, posture, or restful sleep for {current_user.username}."
        )
    elif payload.task_type == "cross_insights":
        recs_summary = "; ".join(f"{r.title} ({r.record_type})" for r in records[:4])
        prompt = (
            "Cross-analyze this patient's lifestyle logs and medical records:\n"
            f"Recent Records: {recs_summary}\n"
            f"Latest Health Log: {f'Water: {today_log.water_glasses}, Sleep: {today_log.sleep_hours}' if today_log else 'No entry today'}\n"
            "Output three clear short sections: UNDERSTAND (key findings), ORGANIZE (what needs follow-up), MANAGE (3 practical actions)."
        )
    elif payload.task_type == "doctor_questions":
        prompt = (
            "Prepare 5 concise, high-value questions a patient should ask their doctor at their upcoming consultation. "
            f"Context: {payload.context_text or 'General health checkup and medicine review.'}"
        )
    else:
        prompt = f"Provide general health information regarding: {payload.context_text or 'routine wellness'}"

    res = await ai_service.generate_response(prompt, language=lang)
    return QuickAiTaskOut(
        task_type=payload.task_type,
        result_text=res["text"],
        disclaimer="AI-generated guidance for patient empowerment. Please discuss medical decisions with your doctor.",
    )

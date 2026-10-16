import os
import json
import uuid
from datetime import datetime
from typing import Optional

import redis
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="SkyCare Portal API")


class ReservationRequest(BaseModel):
    flight_id: str
    passenger_name: str
    passenger_phone: str
    force_fail: Optional[bool] = False


REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
QUEUE_NAME = os.getenv("QUEUE_NAME", "reservation_queue")
DLQ_NAME = os.getenv("DLQ_NAME", "reservation_dlq")

DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = int(os.getenv("DB_PORT", "5432"))

FALLBACK_ALTERNATIVES = {
    "ABL2026": [
        {"flightNo": "LCC912", "departureTime": "18:20", "arrivalTime": "19:30", "remainingSeats": 4, "delayHours": "+3시간 50분"},
        {"flightNo": "LCC934", "departureTime": "19:10", "arrivalTime": "20:20", "remainingSeats": 8, "delayHours": "+4시간 40분"},
        {"flightNo": "LCC958", "departureTime": "21:00", "arrivalTime": "22:10", "remainingSeats": 12, "delayHours": "+6시간 30분"},
    ],
    "JEJU1004": [
        {"flightNo": "LCC831", "departureTime": "16:50", "arrivalTime": "17:55", "remainingSeats": 2, "delayHours": "+5시간 35분"},
        {"flightNo": "LCC855", "departureTime": "18:30", "arrivalTime": "19:35", "remainingSeats": 7, "delayHours": "+7시간 15분"},
    ],
    "LCC7788": [
        {"flightNo": "LCC501", "departureTime": "내일 08:30", "arrivalTime": "내일 09:50", "remainingSeats": 3, "delayHours": "다음날 출발"},
        {"flightNo": "LCC515", "departureTime": "내일 10:45", "arrivalTime": "내일 12:05", "remainingSeats": 9, "delayHours": "다음날 출발"},
    ],
}


def get_redis_client():
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )


def get_db_connection():
    if not DB_HOST or not DB_PASSWORD:
        raise RuntimeError("DB_HOST 또는 DB_PASSWORD 환경변수가 설정되지 않았습니다.")

    return psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT,
        connect_timeout=5,
    )


def init_db_if_possible():
    if os.getenv("AUTO_INIT_DB", "true").lower() != "true":
        return

    try:
        conn = get_db_connection()
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS alternative_flights (
                id SERIAL PRIMARY KEY,
                reservation_no VARCHAR(30) NOT NULL,
                flight_no VARCHAR(30) UNIQUE NOT NULL,
                departure_time VARCHAR(50) NOT NULL,
                arrival_time VARCHAR(50) NOT NULL,
                remaining_seats INTEGER NOT NULL DEFAULT 0 CHECK (remaining_seats >= 0),
                delay_hours VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                id SERIAL PRIMARY KEY,
                request_id VARCHAR(80) UNIQUE NOT NULL,
                flight_id VARCHAR(30) NOT NULL,
                passenger_name VARCHAR(100) NOT NULL,
                passenger_phone VARCHAR(30) NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'BOOKED',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        for reservation_no, flights in FALLBACK_ALTERNATIVES.items():
            for flight in flights:
                cur.execute("""
                    INSERT INTO alternative_flights (
                        reservation_no, flight_no, departure_time, arrival_time,
                        remaining_seats, delay_hours
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (flight_no) DO NOTHING;
                """, (
                    reservation_no,
                    flight["flightNo"],
                    flight["departureTime"],
                    flight["arrivalTime"],
                    flight["remainingSeats"],
                    flight["delayHours"],
                ))

        cur.close()
        conn.close()
        print("[startup] RDS table init/seed completed")
    except Exception as e:
        print(f"[startup] RDS init skipped: {e}")


@app.on_event("startup")
def on_startup():
    init_db_if_possible()


@app.get("/", response_class=HTMLResponse)
def home():
    return r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>항공편 지연·결항 셀프 처리 서비스 | SkyCare Portal</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/lucide@latest"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Noto+Sans+KR:wght@400;500;700;800;900&display=swap');
    body { font-family: 'Inter','Noto Sans KR',system-ui,sans-serif; background: #f8fafc; }
    .glass { background: rgba(255,255,255,.92); backdrop-filter: blur(14px); }
    .fade { animation: fade .22s ease-out; }
    @keyframes fade { from { opacity:0; transform:translateY(6px)} to {opacity:1; transform:translateY(0)} }
    .soft-shadow { box-shadow: 0 12px 34px rgba(15,23,42,.08); }
  </style>
</head>
<body class="min-h-screen text-slate-800">
  <header class="sticky top-0 z-40 glass border-b border-slate-200">
    <div class="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
      <button onclick="goHome()" class="flex items-center gap-3">
        <div class="w-9 h-9 rounded-2xl bg-blue-700 text-white flex items-center justify-center">
          <i data-lucide="plane" class="w-5 h-5 rotate-45"></i>
        </div>
        <div class="text-left">
          <p class="text-sm font-black tracking-tight">SkyCare Portal</p>
          <p class="text-[10px] text-slate-400 font-bold">항공편 지연·결항 셀프 처리</p>
        </div>
      </button>

      <nav class="hidden md:flex items-center gap-2 text-xs font-bold">
        <button id="top-lookup" onclick="switchTab('lookup')" class="px-4 h-10 rounded-xl">예약 조회</button>
        <button id="top-status" onclick="switchTab('status')" class="px-4 h-10 rounded-xl">처리 상태 확인</button>
        <button id="top-help" onclick="switchTab('help')" class="px-4 h-10 rounded-xl">고객 도움</button>
      </nav>

      <div class="hidden sm:flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-50 border border-emerald-100 text-emerald-700 text-[11px] font-black">
        <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
        실시간 처리 가능
      </div>
    </div>
  </header>

  <main class="max-w-6xl mx-auto px-4 py-6 md:py-10 pb-24">
    <div id="app"></div>
  </main>

  <nav class="md:hidden fixed bottom-0 left-0 right-0 bg-white border-t border-slate-200 h-16 z-40 grid grid-cols-3 soft-shadow">
    <button id="mob-lookup" onclick="switchTab('lookup')" class="text-xs font-bold flex flex-col items-center justify-center gap-1"><i data-lucide="search" class="w-5 h-5"></i>예약 조회</button>
    <button id="mob-status" onclick="switchTab('status')" class="text-xs font-bold flex flex-col items-center justify-center gap-1"><i data-lucide="clipboard-list" class="w-5 h-5"></i>처리 상태</button>
    <button id="mob-help" onclick="switchTab('help')" class="text-xs font-bold flex flex-col items-center justify-center gap-1"><i data-lucide="headphones" class="w-5 h-5"></i>고객 도움</button>
  </nav>

  <div id="toast" class="fixed top-20 right-4 z-50 flex flex-col gap-2 w-[calc(100%-2rem)] max-w-sm"></div>
  <div id="modal" class="hidden fixed inset-0 z-50 bg-slate-950/60 backdrop-blur-sm p-4 items-center justify-center"></div>

<script>
const mockFlights = [
  { reservationNo:'ABL2026', passengerName:'김지훈', phoneLast4:'2026', flightNo:'LCC728', from:'김포', to:'제주', originalDeparture:'14:30', newDeparture:'17:10', status:'DELAYED', reason:'항공기 연결 지연', refundAmount:89000 },
  { reservationNo:'JEJU1004', passengerName:'박민서', phoneLast4:'1004', flightNo:'LCC305', from:'김포', to:'제주', originalDeparture:'11:15', newDeparture:'15:40', status:'DELAYED', reason:'상대 공항 기상 악화', refundAmount:72000 },
  { reservationNo:'LCC7788', passengerName:'이도윤', phoneLast4:'7788', flightNo:'LCC402', from:'인천', to:'후쿠오카', originalDeparture:'09:10', newDeparture:'결항', status:'CANCELLED', reason:'태풍 영향으로 인한 기상 악화', refundAmount:145000 }
];

const fallbackAlternatives = {
  ABL2026: [
    { flightNo:'LCC912', departureTime:'18:20', arrivalTime:'19:30', remainingSeats:4, delayHours:'+3시간 50분' },
    { flightNo:'LCC934', departureTime:'19:10', arrivalTime:'20:20', remainingSeats:8, delayHours:'+4시간 40분' },
    { flightNo:'LCC958', departureTime:'21:00', arrivalTime:'22:10', remainingSeats:12, delayHours:'+6시간 30분' }
  ],
  JEJU1004: [
    { flightNo:'LCC831', departureTime:'16:50', arrivalTime:'17:55', remainingSeats:2, delayHours:'+5시간 35분' },
    { flightNo:'LCC855', departureTime:'18:30', arrivalTime:'19:35', remainingSeats:7, delayHours:'+7시간 15분' }
  ],
  LCC7788: [
    { flightNo:'LCC501', departureTime:'내일 08:30', arrivalTime:'내일 09:50', remainingSeats:3, delayHours:'다음날 출발' },
    { flightNo:'LCC515', departureTime:'내일 10:45', arrivalTime:'내일 12:05', remainingSeats:9, delayHours:'다음날 출발' }
  ]
};

const state = {
  tab:'lookup',
  page:'search',
  current:null,
  selectedAlt:null,
  alternatives:{...fallbackAlternatives},
  statusResNo:'ABL2026',
  statusPhone:'2026',
  statusSearched:false,
  actions: [],
  supportCategory:'유아 동반',
  cancelTarget:null
};

function seedActions() {
  const saved = localStorage.getItem('skycare_actions_v2');
  if (saved) {
    try { state.actions = JSON.parse(saved); return; } catch(e) {}
  }
  state.actions = [
    {
      id:'REQ-001', reservationNo:'ABL2026', type:'REBOOK', status:'BOOKED',
      requestedAt:'2026-06-10 14:10', flightNo:'LCC912', from:'김포', to:'제주',
      departureTime:'18:20', arrivalTime:'19:30',
      next:'변경된 항공편 출발 60분 전까지 공항에 도착해 주세요.'
    },
    {
      id:'REQ-002', reservationNo:'ABL2026', type:'REFUND', status:'REFUNDED',
      requestedAt:'2026-06-10 15:00', flightNo:'LCC728', amount:89000,
      next:'환불은 기존 결제 수단으로 처리되었습니다.'
    }
  ];
  saveActions();
}

function saveActions(){ localStorage.setItem('skycare_actions_v2', JSON.stringify(state.actions)); }

function toast(msg, type='success') {
  const wrap = document.getElementById('toast');
  const id = 't' + Date.now();
  const style = type === 'error' ? 'bg-rose-600' : type === 'info' ? 'bg-blue-700' : 'bg-slate-900';
  wrap.insertAdjacentHTML('beforeend', `
    <div id="${id}" class="${style} text-white rounded-2xl px-4 py-3 soft-shadow fade flex items-center gap-3 text-xs font-bold">
      <i data-lucide="${type==='error'?'alert-circle':'check-circle'}" class="w-4 h-4 shrink-0"></i>
      <span>${msg}</span>
    </div>
  `);
  lucide.createIcons();
  setTimeout(() => document.getElementById(id)?.remove(), 3200);
}

function icon(name, cls='w-5 h-5'){ return `<i data-lucide="${name}" class="${cls}"></i>`; }

function calculateAirportArrivalGuide(time) {
  const raw = time.replace('내일 ', '');
  const parts = raw.split(':');
  if (parts.length !== 2) return {recommendedArrivalTime:'-', checkInDeadline:'-', gateMoveTime:'-'};
  const h = parseInt(parts[0],10), m = parseInt(parts[1],10);
  const total = h*60 + m;
  const fmt = (mins) => {
    mins = ((mins % 1440) + 1440) % 1440;
    const hh = String(Math.floor(mins/60)).padStart(2,'0');
    const mm = String(mins%60).padStart(2,'0');
    return `${hh}:${mm}`;
  };
  return {
    recommendedArrivalTime: fmt(total-60),
    checkInDeadline: fmt(total-30),
    gateMoveTime: fmt(total-20)
  };
}

function statusText(s) {
  return {
    BOOKED:'예약 완료',
    CANCELLATION_REQUESTED:'취소 처리중',
    CANCELLED:'취소 완료',
    REFUND_REQUESTED:'환불 요청 접수',
    REFUND_PROCESSING:'환불 처리중',
    REFUNDED:'환불 완료',
    WAITLIST_REGISTERED:'대기 등록 완료',
    STAFF_REVIEWING:'직원 확인중',
    SUPPORT_COMPLETED:'도움 요청 완료',
    WHEELCHAIR_REQUESTED:'휠체어 지원 접수 완료',
    WHEELCHAIR_COMPLETED:'휠체어 지원 완료'
  }[s] || s;
}

function typeText(t) {
  return {
    REBOOK:'대체편 예약',
    CANCEL_REBOOK:'대체편 예약 취소',
    REFUND:'환불 요청',
    WAITLIST:'대기 등록',
    SUPPORT:'직원 도움 요청',
    WHEELCHAIR:'휠체어 지원 신청'
  }[t] || t;
}

function setNav() {
  ['lookup','status','help'].forEach(t => {
    const active = state.tab === t;
    const cls = active ? 'bg-blue-700 text-white shadow-sm' : 'text-slate-500 hover:bg-slate-100';
    const mcls = active ? 'text-blue-700 bg-blue-50' : 'text-slate-400';
    document.getElementById('top-'+t)?.setAttribute('class', `px-4 h-10 rounded-xl ${cls}`);
    document.getElementById('mob-'+t)?.setAttribute('class', `text-xs font-bold flex flex-col items-center justify-center gap-1 ${mcls}`);
  });
}

function switchTab(tab) {
  state.tab = tab;
  state.page = tab === 'lookup' ? 'search' : tab;
  render();
}

function goHome() {
  state.tab = 'lookup';
  state.page = 'search';
  state.current = null;
  render();
}

async function loadAlternatives() {
  try {
    const res = await fetch('/alternatives');
    if (res.ok) state.alternatives = await res.json();
  } catch(e) {
    state.alternatives = {...fallbackAlternatives};
  }
}

function renderInfoPill(label, value) {
  return `
    <div class="rounded-2xl bg-slate-50 border border-slate-200 p-3">
      <p class="text-[10px] font-black text-slate-400 mb-1">${label}</p>
      <p class="text-sm font-black text-slate-800">${value}</p>
    </div>
  `;
}

function renderSearch() {
  return `
    <section class="fade max-w-5xl mx-auto grid lg:grid-cols-[1.05fr_.95fr] gap-6 items-start">
      <div class="pt-4">
        <span class="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-blue-50 text-blue-700 text-xs font-black border border-blue-100 mb-4">
          ${icon('shield-check','w-4 h-4')} 지연·결항 안내를 받은 승객 전용
        </span>
        <h1 class="text-3xl md:text-5xl font-black tracking-tight text-slate-900 leading-tight">
          지연·결항 항공편 처리
        </h1>
        <p class="mt-4 text-slate-500 text-sm md:text-base leading-relaxed">
          예약번호를 입력하고 대체편 예약, 환불, 대기 등록을 직접 진행하세요.
        </p>

        <div class="mt-6 grid sm:grid-cols-3 gap-3">
          ${['ABL2026 / 김지훈 / 2026','JEJU1004 / 박민서 / 1004','LCC7788 / 이도윤 / 7788'].map((x,i)=>`
            <button onclick="fillDemo(${i})" class="text-left bg-white border border-slate-200 rounded-2xl p-4 hover:border-blue-300 hover:soft-shadow transition">
              <p class="text-[10px] font-black text-slate-400 mb-1">시연 데이터</p>
              <p class="text-xs font-black text-blue-700">${x}</p>
            </button>
          `).join('')}
        </div>
      </div>

      <div class="bg-white rounded-[2rem] border border-slate-200 p-5 md:p-7 soft-shadow">
        <form onsubmit="submitSearch(event)" class="space-y-4">
          <div>
            <label class="text-xs font-black text-slate-600">예약번호</label>
            <input id="resNo" required placeholder="예: ABL2026" class="mt-1 w-full h-12 rounded-2xl border border-slate-200 px-4 text-sm font-bold uppercase outline-none focus:border-blue-600" />
          </div>
          <div>
            <label class="text-xs font-black text-slate-600">성함</label>
            <input id="passName" required placeholder="예: 김지훈" class="mt-1 w-full h-12 rounded-2xl border border-slate-200 px-4 text-sm font-bold outline-none focus:border-blue-600" />
          </div>
          <div>
            <label class="text-xs font-black text-slate-600">휴대폰 번호 뒤 4자리</label>
            <input id="phoneLast" maxlength="4" required placeholder="예: 2026" class="mt-1 w-full h-12 rounded-2xl border border-slate-200 px-4 text-sm font-bold outline-none focus:border-blue-600" />
          </div>
          <button class="w-full h-13 rounded-2xl bg-blue-700 hover:bg-blue-800 text-white font-black text-sm flex items-center justify-center gap-2">
            내 항공편 확인하기 ${icon('arrow-right','w-4 h-4')}
          </button>
        </form>
        <p class="mt-4 text-xs text-slate-400 leading-relaxed">
          예약번호는 항공권 예매 내역 또는 문자 안내에서 확인할 수 있습니다.
        </p>
      </div>
    </section>
  `;
}

function fillDemo(idx) {
  const f = mockFlights[idx];
  document.getElementById('resNo').value = f.reservationNo;
  document.getElementById('passName').value = f.passengerName;
  document.getElementById('phoneLast').value = f.phoneLast4;
  toast('시연 데이터가 입력되었습니다.', 'info');
}

function submitSearch(e) {
  e.preventDefault();
  const resNo = document.getElementById('resNo').value.trim().toUpperCase();
  const name = document.getElementById('passName').value.trim();
  const phone = document.getElementById('phoneLast').value.trim();
  const found = mockFlights.find(f => f.reservationNo === resNo && f.passengerName === name && f.phoneLast4 === phone);

  if (!found) {
    toast('일치하는 지연·결항 예약 정보를 찾을 수 없습니다.', 'error');
    return;
  }

  state.current = found;
  state.statusResNo = resNo;
  state.statusPhone = phone;
  state.statusSearched = true;
  state.page = 'flight';
  toast('예약 조회가 완료되었습니다.');
  render();
}

function renderFlightStatus() {
  const f = state.current;
  const message = f.status === 'CANCELLED'
    ? '고객님의 항공편이 결항되었습니다. 아래 옵션 중 하나를 선택해 주세요.'
    : '고객님의 항공편이 지연되었습니다. 아래 옵션 중 하나를 선택해 주세요.';

  const cards = [
    ['REBOOK','refresh-cw','대체편 예약','가장 빠른 다음 항공편으로 변경합니다.','blue'],
    ['REFUND','rotate-ccw','환불 요청','기존 항공권 환불을 요청합니다.','rose'],
    ['WAITLIST','bell','대기 등록','빈 좌석이 생기면 알림을 받습니다.','emerald'],
    ['SUPPORT','headphones','직원 도움 요청','직원의 확인이 필요한 경우 요청합니다.','slate']
  ];

  return `
    <section class="fade max-w-4xl mx-auto">
      <button onclick="goHome()" class="mb-4 text-xs font-black text-slate-500 flex items-center gap-1">${icon('chevron-left','w-4 h-4')} 다시 조회</button>
      <div class="bg-white rounded-[2rem] border border-slate-200 p-5 md:p-7 soft-shadow">
        <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-5">
          <div>
            <p class="text-xs font-black text-blue-700 mb-1">항공편 상태 확인</p>
            <h2 class="text-2xl font-black text-slate-900">${message}</h2>
          </div>
          <span class="px-4 py-2 rounded-full text-xs font-black ${f.status==='CANCELLED'?'bg-rose-50 text-rose-700 border border-rose-100':'bg-amber-50 text-amber-700 border border-amber-100'}">
            ${f.status==='CANCELLED'?'결항':'지연'}
          </span>
        </div>

        <div class="grid sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
          ${renderInfoPill('예약번호', f.reservationNo)}
          ${renderInfoPill('승객명', f.passengerName)}
          ${renderInfoPill('기존 항공편', f.flightNo)}
          ${renderInfoPill('구간', `${f.from} → ${f.to}`)}
          ${renderInfoPill('기존 출발 시간', f.originalDeparture)}
          ${renderInfoPill('변경 출발 시간', f.newDeparture)}
          ${renderInfoPill('사유', f.reason)}
          ${renderInfoPill('처리 가능 메뉴', '대체편·환불·대기')}
        </div>
      </div>

      <div class="grid md:grid-cols-2 gap-4 mt-5">
        ${cards.map(c => `
          <button onclick="selectAction('${c[0]}')" class="bg-white border border-slate-200 hover:border-blue-300 rounded-[1.5rem] p-5 text-left soft-shadow transition">
            <div class="w-11 h-11 rounded-2xl bg-${c[4]}-50 text-${c[4]}-700 flex items-center justify-center mb-4">${icon(c[1])}</div>
            <h3 class="text-base font-black text-slate-900">${c[2]}</h3>
            <p class="text-xs text-slate-500 mt-1 leading-relaxed">${c[3]}</p>
            <div class="mt-4 text-xs font-black text-blue-700 flex items-center gap-1">선택하기 ${icon('arrow-right','w-4 h-4')}</div>
          </button>
        `).join('')}
      </div>
    </section>
  `;
}

function selectAction(action) {
  if (action === 'REBOOK') state.page = 'alternatives';
  if (action === 'REFUND') state.page = 'refund';
  if (action === 'WAITLIST') state.page = 'waitlist';
  if (action === 'SUPPORT') { state.tab = 'help'; state.page = 'support'; }
  render();
}

function renderGuide(alt, compact=false) {
  const g = calculateAirportArrivalGuide(alt.departureTime);
  return `
    <div class="${compact ? 'mt-3' : 'mt-4'} rounded-2xl bg-blue-50/70 border border-blue-100 p-4">
      <p class="text-xs font-black text-blue-900 mb-3 flex items-center gap-2">${icon('clock','w-4 h-4')} 공항 도착 시간 안내</p>
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
        <div><b class="text-slate-500">권장 도착</b><p class="font-black text-slate-900">${g.recommendedArrivalTime}</p></div>
        <div><b class="text-slate-500">체크인 마감</b><p class="font-black text-slate-900">${g.checkInDeadline}</p></div>
        <div><b class="text-slate-500">탑승구 이동</b><p class="font-black text-slate-900">${g.gateMoveTime}</p></div>
      </div>
      <p class="mt-3 text-[11px] text-blue-800 font-bold">국내선 기준으로 출발 60분 전 공항 도착을 권장합니다.</p>
    </div>
  `;
}

function renderAlternatives() {
  const f = state.current;
  const list = state.alternatives[f.reservationNo] || [];
  return `
    <section class="fade max-w-4xl mx-auto">
      <button onclick="state.page='flight';render()" class="mb-4 text-xs font-black text-slate-500 flex items-center gap-1">${icon('chevron-left','w-4 h-4')} 옵션 다시 선택</button>
      <div class="mb-5">
        <h2 class="text-3xl font-black text-slate-900">대체편 선택</h2>
        <p class="text-sm text-slate-500 mt-1">${f.from} → ${f.to} 대체편을 선택해 주세요.</p>
      </div>
      <div class="space-y-4">
        ${list.map(alt => `
          <div class="bg-white rounded-[1.75rem] border border-slate-200 p-5 soft-shadow">
            <div class="flex flex-col md:flex-row md:items-start md:justify-between gap-4">
              <div>
                <div class="flex items-center gap-2 mb-2">
                  <h3 class="text-xl font-black text-slate-900">${alt.flightNo}</h3>
                  <span class="text-[11px] font-black px-2 py-1 rounded-full bg-amber-50 text-amber-700 border border-amber-100">${alt.delayHours}</span>
                </div>
                <div class="grid grid-cols-3 gap-3 text-xs">
                  <div><p class="text-slate-400 font-black">출발</p><p class="text-base font-black">${alt.departureTime}</p></div>
                  <div><p class="text-slate-400 font-black">도착</p><p class="text-base font-black">${alt.arrivalTime}</p></div>
                  <div><p class="text-slate-400 font-black">잔여 좌석</p><p class="text-base font-black text-rose-600">${alt.remainingSeats}석</p></div>
                </div>
              </div>
              <button onclick="openReservationModal('${alt.flightNo}')" class="h-12 px-5 rounded-2xl bg-blue-700 hover:bg-blue-800 text-white text-xs font-black">
                이 항공편으로 변경
              </button>
            </div>
            ${renderGuide(alt, true)}
          </div>
        `).join('')}
      </div>
    </section>
  `;
}

function openReservationModal(flightNo) {
  const alt = (state.alternatives[state.current.reservationNo] || []).find(a => a.flightNo === flightNo);
  state.selectedAlt = alt;
  const guide = calculateAirportArrivalGuide(alt.departureTime);
  const modal = document.getElementById('modal');
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  modal.innerHTML = `
    <div class="bg-white rounded-[2rem] p-6 w-full max-w-md soft-shadow fade">
      <h3 class="text-xl font-black text-slate-900">대체편 예약을 진행할까요?</h3>
      <p class="text-xs text-slate-500 mt-1">선택한 대체편은 출발 60분 전까지 공항 도착을 권장합니다.</p>
      <div class="mt-5 space-y-2 text-sm">
        ${row('기존 항공편', state.current.flightNo)}
        ${row('변경 항공편', alt.flightNo)}
        ${row('변경 출발 시간', alt.departureTime)}
        ${row('변경 도착 시간', alt.arrivalTime)}
        ${row('권장 공항 도착', guide.recommendedArrivalTime)}
        ${row('체크인 마감 예상', guide.checkInDeadline)}
        ${row('탑승구 이동 권장', guide.gateMoveTime)}
      </div>
      <div class="grid grid-cols-2 gap-3 mt-6">
        <button onclick="closeModal()" class="h-12 rounded-2xl bg-slate-100 font-black text-xs">취소</button>
        <button onclick="requestReservation()" class="h-12 rounded-2xl bg-blue-700 text-white font-black text-xs">예약 요청하기</button>
      </div>
    </div>
  `;
};

function row(k,v){ return `<div class="flex justify-between gap-4 py-2 border-b border-slate-100"><span class="text-slate-500 font-bold">${k}</span><b>${v}</b></div>`; }
function closeModal(){ const m=document.getElementById('modal'); m.classList.add('hidden'); m.classList.remove('flex'); }

async function requestReservation() {
  closeModal();
  toast('대체편 예약 요청이 접수되었습니다.', 'info');
  try {
    await fetch('/queue/reservations', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        flight_id: state.selectedAlt.flightNo,
        passenger_name: state.current.passengerName,
        passenger_phone: state.current.phoneLast4,
        force_fail: false
      })
    });
  } catch(e) {}
  state.page = 'processing';
  render();
  runProgress('reservation');
}

function renderProgress(title='대체편 예약 처리중', steps=[]) {
  return `
    <section class="fade max-w-xl mx-auto bg-white rounded-[2rem] border border-slate-200 p-6 soft-shadow text-center">
      <div class="w-14 h-14 rounded-full bg-blue-50 text-blue-700 mx-auto flex items-center justify-center mb-4">
        ${icon('loader-2','w-7 h-7 animate-spin')}
      </div>
      <h2 class="text-2xl font-black">${title}</h2>
      <p class="text-xs text-slate-500 mt-1">잠시만 기다려 주세요. 요청 내용을 차례대로 확인하고 있습니다.</p>
      <div id="progressSteps" class="mt-6 text-left space-y-3">
        ${steps.map((s,i)=>`
          <div id="step-${i}" class="flex items-center gap-3 p-3 rounded-2xl bg-slate-50 border border-slate-100 text-slate-400">
            <span class="w-7 h-7 rounded-full bg-white border flex items-center justify-center text-xs font-black">${i+1}</span>
            <b class="text-xs">${s}</b>
          </div>
        `).join('')}
      </div>
    </section>
  `;
}

function runProgress(kind) {
  const steps = kind === 'cancel'
    ? ['취소 요청 접수 완료','예약 정보 확인 중','대체편 예약 해제 중','취소 확정 중','취소 완료','취소 완료 알림 발송']
    : ['예약 요청 접수 완료','좌석 확인 중','중복 예약 여부 확인 중','대체편 예약 확정 중','예약 완료','예약 완료 알림 발송'];

  let i = 0;
  const timer = setInterval(() => {
    const el = document.getElementById(`step-${i}`);
    if (el) {
      el.className = 'flex items-center gap-3 p-3 rounded-2xl bg-emerald-50 border border-emerald-100 text-emerald-700';
      el.querySelector('span').innerHTML = '✓';
    }
    i++;
    if (i >= steps.length) {
      clearInterval(timer);
      setTimeout(() => kind === 'cancel' ? finishCancel() : finishReservation(), 500);
    }
  }, 650);
}

function finishReservation() {
  const alt = state.selectedAlt;
  state.actions.unshift({
    id:'REQ-' + Math.floor(1000 + Math.random()*9000),
    reservationNo:state.current.reservationNo,
    type:'REBOOK',
    status:'BOOKED',
    requestedAt:new Date().toLocaleString('ko-KR'),
    flightNo:alt.flightNo,
    from:state.current.from,
    to:state.current.to,
    departureTime:alt.departureTime,
    arrivalTime:alt.arrivalTime,
    next:'변경된 항공편 출발 60분 전까지 공항에 도착해 주세요.'
  });
  saveActions();
  toast('대체편 예약이 완료되었습니다.');
  state.page = 'complete';
  render();
}

function renderComplete() {
  const alt = state.selectedAlt;
  return `
    <section class="fade max-w-2xl mx-auto bg-white rounded-[2rem] border border-slate-200 p-6 soft-shadow">
      <div class="text-center">
        <div class="w-16 h-16 rounded-full bg-emerald-50 text-emerald-600 flex items-center justify-center mx-auto mb-4">${icon('check','w-8 h-8')}</div>
        <h2 class="text-2xl font-black">대체편 예약이 완료되었습니다.</h2>
        <p class="text-sm text-slate-500 mt-1">예약 성공 알림이 문자 또는 이메일로 발송되었습니다.</p>
      </div>
      <div class="mt-6 grid sm:grid-cols-2 gap-3">
        ${renderInfoPill('변경된 항공편', alt.flightNo)}
        ${renderInfoPill('구간', `${state.current.from} → ${state.current.to}`)}
        ${renderInfoPill('출발 시간', alt.departureTime)}
        ${renderInfoPill('도착 시간', alt.arrivalTime)}
        ${renderInfoPill('예약 상태', '예약 완료')}
        ${renderInfoPill('알림 상태', '예약 완료 알림 발송 완료')}
      </div>
      ${renderGuide(alt)}
      <div class="grid grid-cols-2 gap-3 mt-6">
        <button onclick="state.tab='status';state.page='status';state.statusSearched=true;render()" class="h-12 rounded-2xl bg-blue-700 text-white text-xs font-black">처리 상태 확인</button>
        <button onclick="goHome()" class="h-12 rounded-2xl bg-slate-100 text-slate-700 text-xs font-black">홈으로 돌아가기</button>
      </div>
    </section>
  `;
}

function renderRefund() {
  const f = state.current;
  return `
    <section class="fade max-w-xl mx-auto bg-white rounded-[2rem] border border-slate-200 p-6 soft-shadow">
      <button onclick="state.page='flight';render()" class="mb-4 text-xs font-black text-slate-500">${icon('chevron-left','w-4 h-4')} 이전</button>
      <h2 class="text-2xl font-black">환불 요청</h2>
      <p class="text-xs text-slate-500 mt-1">환불 금액과 완료 시점은 항공사 정책 및 결제 수단에 따라 달라질 수 있습니다.</p>
      <div class="mt-5 space-y-2">
        ${row('환불 대상 항공편', f.flightNo)}
        ${row('예약번호', f.reservationNo)}
        ${row('승객명', f.passengerName)}
        ${row('환불 사유', '항공편 지연/결항')}
        ${row('예상 환불 금액', f.refundAmount.toLocaleString() + '원')}
        ${row('환불 수단', '기존 결제 수단')}
      </div>
      <button onclick="submitRefund()" class="mt-6 w-full h-12 rounded-2xl bg-rose-600 hover:bg-rose-700 text-white text-xs font-black">환불 요청하기</button>
    </section>
  `;
}

function submitRefund() {
  const f = state.current;
  toast('환불 요청이 접수되었습니다.', 'info');
  state.actions.unshift({
    id:'REF-' + Math.floor(1000 + Math.random()*9000),
    reservationNo:f.reservationNo, type:'REFUND', status:'REFUNDED',
    requestedAt:new Date().toLocaleString('ko-KR'), flightNo:f.flightNo, amount:f.refundAmount,
    next:'환불은 기존 결제 수단으로 처리됩니다.'
  });
  saveActions();
  setTimeout(()=>toast('환불 완료 처리되었습니다.'), 500);
  state.tab='status'; state.page='status'; state.statusSearched=true; render();
}

function renderWaitlist() {
  return `
    <section class="fade max-w-xl mx-auto bg-white rounded-[2rem] border border-slate-200 p-6 soft-shadow">
      <button onclick="state.page='flight';render()" class="mb-4 text-xs font-black text-slate-500">${icon('chevron-left','w-4 h-4')} 이전</button>
      <h2 class="text-2xl font-black">대기 등록</h2>
      <p class="text-xs text-slate-500 mt-1">좌석이 발생하면 선택한 알림 수단으로 안내드립니다.</p>
      <div class="mt-5 space-y-4">
        <div>
          <p class="text-xs font-black mb-2">희망 시간대</p>
          ${['가장 빠른 항공편','저녁 시간대','다음날 오전'].map((x,i)=>`<label class="flex gap-2 p-3 border rounded-2xl mb-2 text-sm font-bold"><input type="radio" name="waitTime" ${i===0?'checked':''}>${x}</label>`).join('')}
        </div>
        <div>
          <p class="text-xs font-black mb-2">알림 수단</p>
          ${['문자','이메일','앱푸시'].map((x,i)=>`<label class="flex gap-2 p-3 border rounded-2xl mb-2 text-sm font-bold"><input type="radio" name="notice" ${i===0?'checked':''}>${x}</label>`).join('')}
        </div>
      </div>
      <button onclick="submitWaitlist()" class="mt-2 w-full h-12 rounded-2xl bg-emerald-600 text-white text-xs font-black">대기 등록하기</button>
    </section>
  `;
}

function submitWaitlist() {
  const f = state.current;
  state.actions.unshift({
    id:'WAIT-' + Math.floor(1000 + Math.random()*9000),
    reservationNo:f.reservationNo, type:'WAITLIST', status:'WAITLIST_REGISTERED',
    requestedAt:new Date().toLocaleString('ko-KR'), flightNo:f.flightNo,
    next:'좌석이 발생하면 선택한 알림 수단으로 안내드립니다.'
  });
  saveActions();
  toast('대기 등록이 완료되었습니다.');
  state.tab='status'; state.page='status'; state.statusSearched=true; render();
}

function renderStatus() {
  const actions = state.statusSearched
    ? state.actions.filter(a => a.reservationNo.toUpperCase() === state.statusResNo.toUpperCase())
    : [];

  return `
    <section class="fade max-w-5xl mx-auto grid lg:grid-cols-[320px_1fr] gap-5">
      <div class="bg-white rounded-[2rem] border border-slate-200 p-5 soft-shadow h-fit">
        <h2 class="text-xl font-black">처리 상태 확인</h2>
        <p class="text-xs text-slate-500 mt-1">예약번호와 휴대폰 뒤 4자리로 요청 상태를 확인하세요.</p>
        <form onsubmit="event.preventDefault();state.statusSearched=true;render();" class="mt-5 space-y-3">
          <input value="${state.statusResNo}" oninput="state.statusResNo=this.value.toUpperCase()" placeholder="예약번호" class="w-full h-12 rounded-2xl border px-4 text-sm font-bold uppercase" />
          <input value="${state.statusPhone}" oninput="state.statusPhone=this.value" maxlength="4" placeholder="휴대폰 뒤 4자리" class="w-full h-12 rounded-2xl border px-4 text-sm font-bold" />
          <button class="w-full h-12 rounded-2xl bg-blue-700 text-white text-xs font-black">조회하기</button>
        </form>
      </div>

      <div class="space-y-4">
        ${!state.statusSearched ? emptyBox('조회 조건을 입력하고 처리 내역을 확인해 주세요.') :
          actions.length === 0 ? emptyBox('표시할 처리 내역이 없습니다.<br>예약번호를 다시 조회하거나 새로운 요청을 진행해 주세요.') :
          actions.map(renderStatusCard).join('')}
      </div>
    </section>
  `;
}

function emptyBox(msg) {
  return `<div class="bg-white rounded-[2rem] border border-slate-200 p-10 text-center text-sm text-slate-500 font-bold">${msg}</div>`;
}

function renderStatusCard(a) {
  const deletable = ['CANCELLED','REFUNDED','WAITLIST_CANCELLED','SUPPORT_COMPLETED','WHEELCHAIR_COMPLETED'].includes(a.status);
  const canCancel = a.type === 'REBOOK' && a.status === 'BOOKED' && a.flightNo;
  const alt = {departureTime:a.departureTime || '18:20', arrivalTime:a.arrivalTime || '19:30', flightNo:a.flightNo};
  return `
    <div class="bg-white rounded-[2rem] border border-slate-200 p-5 soft-shadow">
      <div class="flex justify-between gap-4 mb-4">
        <div>
          <p class="text-[11px] font-black text-blue-700 mb-1">${typeText(a.type)}</p>
          <h3 class="text-xl font-black">${statusText(a.status)}</h3>
        </div>
        <span class="text-[10px] font-black text-slate-400">${a.id}</span>
      </div>
      <div class="grid sm:grid-cols-2 gap-3">
        ${renderInfoPill('예약번호', a.reservationNo)}
        ${renderInfoPill('현재 상태', statusText(a.status))}
        ${a.flightNo ? renderInfoPill(a.status==='CANCELLED'?'취소된 항공편':'항공편명', a.flightNo) : ''}
        ${a.from ? renderInfoPill('구간', `${a.from} → ${a.to}`) : ''}
        ${a.departureTime ? renderInfoPill('출발 시간', a.departureTime) : ''}
        ${a.arrivalTime ? renderInfoPill('도착 시간', a.arrivalTime) : ''}
        ${a.amount ? renderInfoPill('환불 금액', a.amount.toLocaleString() + '원') : ''}
        ${renderInfoPill('요청/처리 시각', a.requestedAt)}
      </div>
      ${canCancel ? renderGuide(alt) : ''}
      <div class="mt-4 rounded-2xl bg-slate-50 border border-slate-200 p-4 text-xs text-slate-600 font-bold">
        다음 안내: ${a.next || '담당자가 요청 내용을 확인하고 있습니다.'}
      </div>
      ${canCancel ? `<button onclick="openCancelModal('${a.id}')" class="mt-4 w-full h-12 rounded-2xl border border-rose-200 text-rose-600 hover:bg-rose-50 text-xs font-black">대체편 예약 취소하기</button>` : ''}
      ${a.status==='CANCELLED' ? `
        <div class="grid sm:grid-cols-4 gap-2 mt-4">
          <button onclick="state.tab='lookup';state.page='alternatives';render()" class="h-10 rounded-xl bg-blue-50 text-blue-700 text-[11px] font-black">다른 대체편 선택</button>
          <button onclick="state.tab='lookup';state.page='refund';render()" class="h-10 rounded-xl bg-rose-50 text-rose-700 text-[11px] font-black">환불 요청</button>
          <button onclick="state.tab='lookup';state.page='waitlist';render()" class="h-10 rounded-xl bg-emerald-50 text-emerald-700 text-[11px] font-black">대기 등록</button>
          <button onclick="goHome()" class="h-10 rounded-xl bg-slate-100 text-slate-700 text-[11px] font-black">홈으로</button>
        </div>
      ` : ''}
      ${deletable ? `<button onclick="deleteAction('${a.id}')" class="mt-3 w-full h-11 rounded-2xl bg-slate-100 text-slate-600 text-xs font-black">내역 삭제</button>` : ''}
    </div>
  `;
}

function openCancelModal(id) {
  state.cancelTarget = state.actions.find(a => a.id === id);
  const a = state.cancelTarget;
  const modal = document.getElementById('modal');
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  modal.innerHTML = `
    <div class="bg-white rounded-[2rem] p-6 w-full max-w-md soft-shadow fade">
      <h3 class="text-xl font-black text-slate-900">대체편 예약을 취소할까요?</h3>
      <p class="text-xs text-slate-500 mt-1 leading-relaxed">취소하면 선택한 대체편 예약이 해제됩니다. 이후 다른 대체편을 다시 선택하거나 환불 요청, 대기 등록을 진행할 수 있습니다.</p>
      <div class="mt-4 space-y-2 text-sm">
        ${row('예약번호', a.reservationNo)}
        ${row('현재 대체편', a.flightNo)}
        ${row('구간', `${a.from} → ${a.to}`)}
        ${row('출발 시간', a.departureTime)}
        ${row('도착 시간', a.arrivalTime)}
      </div>
      <div class="mt-4">
        <p class="text-xs font-black text-slate-700 mb-2">취소 사유</p>
        <select id="cancelReason" class="w-full h-12 rounded-2xl border px-3 text-sm font-bold">
          <option value="">선택해 주세요</option>
          <option>더 빠른 항공편을 다시 선택하고 싶어요</option>
          <option>여행 일정을 취소했어요</option>
          <option>환불을 요청하고 싶어요</option>
          <option>시간이 맞지 않아요</option>
          <option>기타</option>
        </select>
      </div>
      <div class="grid grid-cols-2 gap-3 mt-6">
        <button onclick="closeModal()" class="h-12 rounded-2xl bg-slate-100 font-black text-xs">아니요, 유지할게요</button>
        <button onclick="submitCancel()" class="h-12 rounded-2xl bg-rose-600 text-white font-black text-xs">예약 취소하기</button>
      </div>
    </div>
  `;
}

function submitCancel() {
  const reason = document.getElementById('cancelReason').value;
  if (!reason) { toast('취소 사유를 선택해 주세요.', 'error'); return; }
  closeModal();
  toast('대체편 예약 취소 요청이 접수되었습니다.', 'info');
  state.page='cancelProgress';
  render();
  runProgress('cancel');
}

function finishCancel() {
  const a = state.cancelTarget;
  const idx = state.actions.findIndex(x => x.id === a.id);
  if (idx >= 0) {
    state.actions[idx] = {
      ...state.actions[idx],
      type:'CANCEL_REBOOK',
      status:'CANCELLED',
      requestedAt:new Date().toLocaleString('ko-KR'),
      next:'다른 대체편을 선택하거나 환불 요청을 진행할 수 있습니다.'
    };
    saveActions();
  }
  toast('대체편 예약이 취소되었습니다.');
  state.tab='status';
  state.page='status';
  state.statusSearched=true;
  render();
}

function deleteAction(id) {
  const modal = document.getElementById('modal');
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  modal.innerHTML = `
    <div class="bg-white rounded-[2rem] p-6 w-full max-w-sm soft-shadow fade">
      <h3 class="text-xl font-black">이 내역을 삭제할까요?</h3>
      <p class="text-xs text-slate-500 mt-2 leading-relaxed">삭제하면 처리 상태 확인 목록에서 해당 내역이 보이지 않습니다. 이미 완료된 처리 결과는 항공사 시스템에 보관될 수 있습니다.</p>
      <div class="grid grid-cols-2 gap-3 mt-6">
        <button onclick="closeModal()" class="h-12 rounded-2xl bg-slate-100 font-black text-xs">취소</button>
        <button onclick="confirmDelete('${id}')" class="h-12 rounded-2xl bg-rose-600 text-white font-black text-xs">내역 삭제</button>
      </div>
    </div>
  `;
}

function confirmDelete(id) {
  state.actions = state.actions.filter(a => a.id !== id);
  saveActions();
  closeModal();
  toast('처리 내역이 목록에서 삭제되었습니다.');
  render();
}

function renderHelp() {
  return `
    <section class="fade max-w-4xl mx-auto">
      <h2 class="text-3xl font-black mb-2">고객 도움 요청</h2>
      <p class="text-sm text-slate-500 mb-5">직원의 확인이 필요한 경우 요청 내용을 남겨 주세요.</p>
      <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
        ${['유아 동반','단체 예약','환승 연결','결제 오류','수하물 문제','기타 문의'].map(x=>`
          <button onclick="state.supportCategory='${x}';state.page='support';render()" class="bg-white rounded-[1.5rem] border border-slate-200 p-5 text-left soft-shadow hover:border-blue-300">
            <h3 class="font-black">${x}</h3>
            <p class="text-xs text-slate-500 mt-1">직원에게 확인을 요청합니다.</p>
          </button>
        `).join('')}
        <button onclick="state.page='wheelchair';render()" class="bg-blue-700 text-white rounded-[1.5rem] p-5 text-left soft-shadow">
          <h3 class="font-black">휠체어 지원 신청</h3>
          <p class="text-xs text-blue-100 mt-1">공항 내 이동 및 탑승 지원을 요청합니다.</p>
        </button>
      </div>
    </section>
  `;
}

function renderSupportForm() {
  return `
    <section class="fade max-w-xl mx-auto bg-white rounded-[2rem] border border-slate-200 p-6 soft-shadow">
      <button onclick="state.page='help';render()" class="mb-4 text-xs font-black text-slate-500">${icon('chevron-left','w-4 h-4')} 이전</button>
      <h2 class="text-2xl font-black">직원 도움 요청</h2>
      <p class="text-xs text-slate-500 mt-1">선택 사유: ${state.supportCategory}</p>
      <textarea id="supportText" rows="5" placeholder="직원에게 전달할 내용을 입력해 주세요." class="mt-5 w-full rounded-2xl border p-4 text-sm outline-none"></textarea>
      <button onclick="submitSupport()" class="mt-4 w-full h-12 rounded-2xl bg-blue-700 text-white text-xs font-black">직원 도움 요청하기</button>
    </section>
  `;
}

function submitSupport() {
  const resNo = state.current?.reservationNo || 'ABL2026';
  state.actions.unshift({
    id:'SUP-' + Math.floor(1000 + Math.random()*9000),
    reservationNo:resNo, type:'SUPPORT', status:'STAFF_REVIEWING',
    requestedAt:new Date().toLocaleString('ko-KR'), next:'담당자가 확인 후 안내드릴 예정입니다.'
  });
  saveActions();
  toast('직원 도움 요청이 접수되었습니다.');
  state.tab='status'; state.page='status'; state.statusSearched=true; render();
}

function renderWheelchair() {
  return `
    <section class="fade max-w-2xl mx-auto bg-white rounded-[2rem] border border-slate-200 p-6 soft-shadow">
      <button onclick="state.page='help';render()" class="mb-4 text-xs font-black text-slate-500">${icon('chevron-left','w-4 h-4')} 이전</button>
      <h2 class="text-2xl font-black">휠체어 지원 신청</h2>
      <p class="text-xs text-slate-500 mt-1">체크인부터 탑승까지 필요한 이동 지원을 요청할 수 있습니다.</p>
      <div class="grid sm:grid-cols-2 gap-3 mt-5">
        <input id="whResNo" placeholder="예약번호" value="${state.current?.reservationNo || 'ABL2026'}" class="h-12 rounded-2xl border px-4 text-sm font-bold">
        <input placeholder="승객명" value="${state.current?.passengerName || '김지훈'}" class="h-12 rounded-2xl border px-4 text-sm font-bold">
        <input placeholder="항공편명" value="${state.current?.flightNo || 'LCC728'}" class="h-12 rounded-2xl border px-4 text-sm font-bold">
        <input placeholder="연락 가능한 휴대폰 번호" class="h-12 rounded-2xl border px-4 text-sm font-bold">
      </div>
      <div class="mt-5 grid sm:grid-cols-2 gap-4">
        <div>
          <p class="text-xs font-black mb-2">지원 대상</p>
          <select class="w-full h-12 rounded-2xl border px-3 text-sm font-bold"><option>본인</option><option>동행 승객</option><option>가족 / 보호자</option></select>
        </div>
        <div>
          <p class="text-xs font-black mb-2">지원 사유</p>
          <select class="w-full h-12 rounded-2xl border px-3 text-sm font-bold"><option>휠체어 이용 승객</option><option>노약자</option><option>일시적 부상</option><option>장거리 보행 어려움</option><option>기타</option></select>
        </div>
      </div>
      <div class="mt-5">
        <p class="text-xs font-black mb-2">필요한 지원 범위</p>
        <div class="grid sm:grid-cols-2 gap-2">
          ${['체크인 카운터까지 이동 지원','보안검색대 이동 지원','탑승구까지 이동 지원','항공기 탑승 지원','도착 후 수하물 수취 구역까지 이동 지원','환승 구간 이동 지원'].map(x=>`
            <label class="flex gap-2 p-3 rounded-2xl border text-xs font-bold"><input type="checkbox">${x}</label>
          `).join('')}
        </div>
      </div>
      <label class="mt-5 flex gap-2 text-xs font-bold text-slate-600">
        <input id="whAgree" type="checkbox"> 휠체어 지원 신청을 위해 입력한 정보가 항공사 직원에게 전달되는 것에 동의합니다.
      </label>
      <button onclick="submitWheelchair()" class="mt-5 w-full h-12 rounded-2xl bg-blue-700 text-white text-xs font-black">휠체어 지원 신청하기</button>
    </section>
  `;
}

function submitWheelchair() {
  if (!document.getElementById('whAgree').checked) {
    toast('개인정보 전달 동의가 필요합니다.', 'error');
    return;
  }
  const resNo = document.getElementById('whResNo').value || 'ABL2026';
  state.actions.unshift({
    id:'WHL-' + Math.floor(1000 + Math.random()*9000),
    reservationNo:resNo, type:'WHEELCHAIR', status:'WHEELCHAIR_REQUESTED',
    requestedAt:new Date().toLocaleString('ko-KR'), next:'항공사 직원이 확인 후 공항에서 이동을 도와드릴 예정입니다.'
  });
  saveActions();
  toast('휠체어 지원 신청이 접수되었습니다.');
  state.tab='status'; state.page='status'; state.statusSearched=true; render();
}

function render() {
  setNav();
  const app = document.getElementById('app');

  if (state.page === 'search') app.innerHTML = renderSearch();
  else if (state.page === 'flight') app.innerHTML = renderFlightStatus();
  else if (state.page === 'alternatives') app.innerHTML = renderAlternatives();
  else if (state.page === 'processing') app.innerHTML = renderProgress('대체편 예약 처리중', ['예약 요청 접수 완료','좌석 확인 중','중복 예약 여부 확인 중','대체편 예약 확정 중','예약 완료','예약 완료 알림 발송']);
  else if (state.page === 'complete') app.innerHTML = renderComplete();
  else if (state.page === 'refund') app.innerHTML = renderRefund();
  else if (state.page === 'waitlist') app.innerHTML = renderWaitlist();
  else if (state.page === 'status') app.innerHTML = renderStatus();
  else if (state.page === 'help') app.innerHTML = renderHelp();
  else if (state.page === 'support') app.innerHTML = renderSupportForm();
  else if (state.page === 'wheelchair') app.innerHTML = renderWheelchair();
  else if (state.page === 'cancelProgress') app.innerHTML = renderProgress('대체편 예약 취소 처리중', ['취소 요청 접수 완료','예약 정보 확인 중','대체편 예약 해제 중','취소 확정 중','취소 완료','취소 완료 알림 발송']);

  lucide.createIcons();
}

window.addEventListener('DOMContentLoaded', async () => {
  seedActions();
  await loadAlternatives();
  render();
});
</script>
</body>
</html>
"""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/alternatives")
def get_alternatives():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT reservation_no, flight_no, departure_time, arrival_time,
                   remaining_seats, delay_hours
            FROM alternative_flights
            ORDER BY reservation_no, departure_time;
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        grouped = {}
        for row in rows:
            grouped.setdefault(row["reservation_no"], []).append({
                "flightNo": row["flight_no"],
                "departureTime": row["departure_time"],
                "arrivalTime": row["arrival_time"],
                "remainingSeats": row["remaining_seats"],
                "delayHours": row["delay_hours"],
            })

        return grouped if grouped else FALLBACK_ALTERNATIVES
    except Exception as e:
        print(f"[alternatives] fallback used: {e}")
        return FALLBACK_ALTERNATIVES


@app.post("/queue/reservations")
def queue_reservation(req: ReservationRequest):
    request_id = f"REQ-{uuid.uuid4().hex[:10].upper()}"
    payload = {
        "request_id": request_id,
        "flight_id": req.flight_id,
        "passenger_name": req.passenger_name,
        "passenger_phone": req.passenger_phone,
        "force_fail": req.force_fail,
        "requested_at": datetime.utcnow().isoformat(),
    }

    try:
        r = get_redis_client()
        r.lpush(QUEUE_NAME, json.dumps(payload, ensure_ascii=False))
        return {
            "status": "queued",
            "request_id": request_id,
            "message": "예약 요청이 접수되었습니다."
        }
    except Exception as e:
        print(f"[queue] redis enqueue failed: {e}")
        raise HTTPException(status_code=500, detail="예약 요청 접수 중 오류가 발생했습니다.")


@app.get("/queue/status")
def queue_status():
    try:
        r = get_redis_client()
        return {
            "queue_name": QUEUE_NAME,
            "queue_length": r.llen(QUEUE_NAME),
            "dlq_name": DLQ_NAME,
            "dlq_length": r.llen(DLQ_NAME),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/reservations")
def list_reservations():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT request_id, flight_id, passenger_name, passenger_phone, status, created_at
            FROM reservations
            ORDER BY created_at DESC
            LIMIT 50;
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

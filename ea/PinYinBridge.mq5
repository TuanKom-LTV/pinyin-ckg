//+------------------------------------------------------------------+
//| PinYin Bridge — nối MT5 của khách với buồng lái PinYin CKG       |
//| Khách bấm lệnh trên web (pinyin.id.vn) → EA này vào lệnh trên    |
//| tài khoản MT5 của chính khách. Mật khẩu MT5 không rời máy khách. |
//+------------------------------------------------------------------+
#property copyright "PinYin CKG"
#property link      "https://pinyin.id.vn"
#property version   "1.70"
#property description "Cầu nối buồng lái PinYin CKG. Dán Mã kết nối lấy trên trang pinyin.id.vn."
#property description "Cần cho phép WebRequest tới https://pinyin.id.vn (Tools > Options > Expert Advisors)."

#include <Trade/Trade.mqh>

input string InpKey    = "";                      // Mã kết nối PinYin (lấy trên trang web)
input string InpUrl    = "https://pinyin.id.vn";  // Địa chỉ PinYin
input string InpSymbol = "";                      // Tên vàng trên sàn (để trống: tự tìm XAUUSD)

#define EA_VER   "1.7"
#define EA_MAGIC 260118

CTrade  trade;
string  Sym = "";
int     hEma[3];
ENUM_TIMEFRAMES TFS[3] = {PERIOD_M1, PERIOD_M5, PERIOD_M15};
string  TFN[3] = {"M1", "M5", "M15"};
bool    gBe = false, gTr = false, gHalf = true;
double  gBeAt = 1.5, gBeLock = 0.25, gTrAt = 2.2, gTrStep = 0.6;
bool    gMan = false, gAuto = true;      // lá chắn cho lệnh vào tay (MT5 điện thoại, máy khác)
double  gSl = 2.5, gTp = 5.0;
string  results = "";          // kết quả lệnh chờ gửi lên máy chủ ở lần đồng bộ sau
string  gStatus  = "Đang kết nối...";
string  doneIds[50];
// Copy lệnh bot: TP1 đặt sẵn trên sàn (lệnh tách phần "PinYin TP1"), EA tự dời hòa vốn khi sàn đã chốt TP1
double  gTp1Dist = 0, gTp1Applied = -1, gAvg0 = 0, gBeOff = 0;
bool    gBeOnTp1 = true, gHadTp1 = false, gTp1Done = false, gSplit = false;
// 1.7: copy NHIỀU bot cùng lúc. Lệnh của bot khác Bot 1 gắn nhãn "#<bot>" trong ghi chú (vd "PinYin #b2", "PinYin #b2 TP1").
// Lệnh máy chủ có trường cuối "T:<bot>" → chỉ áp lên lệnh mang đúng nhãn đó. Không nhãn = Bot 1 (như các bản cũ).
string  gTag = "", gTp1Tag = "";
int     gNPos = -1;
int     doneN = 0;

//---------------- Tiện ích ----------------
string Clean(string s) { StringReplace(s, "|", "/"); StringReplace(s, "\n", " "); StringReplace(s, "\r", " "); return s; }
string D(double v, int dg) { return DoubleToString(v, dg); }
int    Dg() { return (int)SymbolInfoInteger(Sym, SYMBOL_DIGITS); }
double Unit() { string u = Sym; StringToUpper(u); if(StringFind(u, "XAU") >= 0 || StringFind(u, "GOLD") >= 0) return 1.0; return SymbolInfoDouble(Sym, SYMBOL_POINT) * 10; }
double MinDist() { return (SymbolInfoInteger(Sym, SYMBOL_TRADE_STOPS_LEVEL) + 1) * SymbolInfoDouble(Sym, SYMBOL_POINT); }

bool AffixOk(string a, bool suf)
{
   int n = StringLen(a);
   if(n == 0) return true;
   if(n > 5)  return false;
   ushort c = suf ? StringGetCharacter(a, 0) : StringGetCharacter(a, n - 1);
   bool alnum = (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
   if(!alnum) return true;
   string lo = a; StringToLower(lo);
   return lo == a;                         // hậu tố chữ thường (m, c, pro) — chặn XAUUSDT
}

string FindSym(string base)
{
   string best = ""; int bestScore = 9999;
   string bases[2]; bases[0] = base; bases[1] = "GOLD";
   int total = SymbolsTotal(false);
   for(int i = 0; i < total; i++)
   {
      string n = SymbolName(i, false), up = n; StringToUpper(up);
      for(int b = 0; b < 2; b++)
      {
         string bs = bases[b]; int L = StringLen(bs), N = StringLen(n), score = 9999;
         if(StringFind(up, bs) == 0 && AffixOk(StringSubstr(n, L), true)) score = (N - L) + b * 20;
         else if(N > L && StringFind(up, bs, N - L) == N - L && AffixOk(StringSubstr(n, 0, N - L), false)) score = (N - L) + b * 20;
         if(score < 9999 && SymbolInfoInteger(n, SYMBOL_VISIBLE)) score -= 1;   // ưu tiên mã đang hiện trong Market Watch
         if(score < bestScore) { bestScore = score; best = n; }
      }
   }
   return best;
}

double NormLot(double lot)
{
   double st = SymbolInfoDouble(Sym, SYMBOL_VOLUME_STEP), mn = SymbolInfoDouble(Sym, SYMBOL_VOLUME_MIN), mx = SymbolInfoDouble(Sym, SYMBOL_VOLUME_MAX);
   if(st <= 0) st = 0.01;
   lot = MathRound(lot / st) * st;
   if(lot < mn) return 0;
   return NormalizeDouble(MathMin(lot, mx), 2);
}

int TfIdx(string tf) { for(int i = 0; i < 3; i++) if(TFN[i] == tf) return i; return 0; }

double Ema(int i) { double b[]; if(CopyBuffer(hEma[i], 0, 0, 1, b) < 1) return 0; return b[0]; }

bool HiLo(int i, double &hi, double &lo)
{
   double h[], l[];
   if(CopyHigh(Sym, TFS[i], 1, 10, h) < 10 || CopyLow(Sym, TFS[i], 1, 10, l) < 10) return false;
   hi = h[ArrayMaximum(h)]; lo = l[ArrayMinimum(l)];
   return true;
}

bool gScope = false;      // true: lệnh từ bot chỉ áp lên lệnh của bot (ghi chú "PinYin BOT"), không đụng lệnh vào tay qua buồng lái
bool IsBotC() { return StringFind(PositionGetString(POSITION_COMMENT), "PinYin BOT") == 0; }
string TagOf()                       // nhãn bot nguồn trong ghi chú lệnh đang chọn ("" = Bot 1 / lệnh không nhãn)
{
   string c = PositionGetString(POSITION_COMMENT);
   int i = StringFind(c, "#"); if(i < 0) return "";
   int j = StringFind(c, " ", i);
   return j < 0 ? StringSubstr(c, i + 1) : StringSubstr(c, i + 1, j - i - 1);
}
string CmT(bool tp1)                 // ghi chú lệnh mới theo phạm vi + nhãn đang xử lý
{
   string b = gScope ? "PinYin BOT" : "PinYin";
   if(gTag != "") b += " #" + gTag;
   if(tp1) b += " TP1";
   return b;
}
bool Mine(ulong ticket) { return PositionSelectByTicket(ticket) && PositionGetInteger(POSITION_MAGIC) == EA_MAGIC && PositionGetString(POSITION_SYMBOL) == Sym && (!gScope || IsBotC()) && TagOf() == gTag; }
bool AnyBot()
{
   bool o = gScope; gScope = true; bool r = false;
   for(int i = 0; i < PositionsTotal() && !r; i++) if(Mine(PositionGetTicket(i))) r = true;
   gScope = o; return r;
}

bool OkRet() { uint r = trade.ResultRetcode(); return r == TRADE_RETCODE_DONE || r == TRADE_RETCODE_PLACED || r == TRADE_RETCODE_DONE_PARTIAL; }
string Rej() { return "MT5 từ chối: " + IntegerToString(trade.ResultRetcode()) + " " + trade.ResultRetcodeDescription(); }

//---------------- Lệnh từ web ----------------
string DoOrder(string side, double lot, string mode, double sl, double tp, double off, string tfs)
{
   bool buy = side == "BUY"; double u = Unit(), mind = MinDist(); int dg = Dg(), ti = TfIdx(tfs);
   lot = NormLot(lot);
   if(lot <= 0) return "0|Lot không hợp lệ với sàn.";
   MqlTick t; if(!SymbolInfoTick(Sym, t) || t.bid <= 0) return "0|Chưa có giá (thị trường có thể đang đóng).";
   double mkt = buy ? t.ask : t.bid, entry = mkt; bool pend = false;
   if(mode == "ema9")
   {
      double e = Ema(ti); if(e <= 0) return "0|Chưa tính được EMA9.";
      entry = buy ? e + off * u : e - off * u; pend = true;
   }
   else if(mode == "zone")
   {
      double hi, lo; if(!HiLo(ti, hi, lo)) return "0|Chưa đủ nến.";
      entry = buy ? lo : hi; pend = true;
      if((buy && entry > mkt) || (!buy && entry < mkt)) return "0|Giá đã vượt cản 10 nến, không đặt lệnh chờ.";
   }
   entry = NormalizeDouble(entry, dg);
   if(pend && MathAbs(entry - mkt) < mind) return "0|Giá vào quá sát giá hiện tại, dùng chế độ NGAY.";
   double s = 0, p = 0;
   if(sl > 0) { double d = MathMax(sl * u, mind); s = NormalizeDouble(buy ? entry - d : entry + d, dg); }
   if(tp > 0) { double d = MathMax(tp * u, mind); p = NormalizeDouble(buy ? entry + d : entry - d, dg); }
   bool ok;
   if(!pend) ok = buy ? trade.Buy(lot, Sym, 0, s, p, CmT(false)) : trade.Sell(lot, Sym, 0, s, p, CmT(false));
   else
   {
      bool below = entry < mkt;
      if(buy) ok = below ? trade.BuyLimit(lot, entry, Sym, s, p, ORDER_TIME_GTC, 0, CmT(false)) : trade.BuyStop(lot, entry, Sym, s, p, ORDER_TIME_GTC, 0, CmT(false));
      else    ok = below ? trade.SellStop(lot, entry, Sym, s, p, ORDER_TIME_GTC, 0, CmT(false)) : trade.SellLimit(lot, entry, Sym, s, p, ORDER_TIME_GTC, 0, CmT(false));
   }
   if(!ok || !OkRet()) return "0|" + Rej();
   double px = pend ? entry : trade.ResultPrice();
   return "1|" + (pend ? "Đã đặt lệnh chờ " : "Đã vào ") + side + " " + D(lot, 2) + " lot @ " + D(px, dg)
          + (s > 0 ? " · SL " + D(s, dg) : "") + (p > 0 ? " · TP " + D(p, dg) : "");
}

string DoClose(ulong tk)
{
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(!Mine(t) || (tk > 0 && t != tk)) continue;
      if(trade.PositionClose(t) && OkRet()) n++; else return "0|" + Rej();
   }
   return n > 0 ? "1|Đã đóng " + IntegerToString(n) + " lệnh." : "0|Không có lệnh mở.";
}

double HalfVol(double vol)
{
   double st = SymbolInfoDouble(Sym, SYMBOL_VOLUME_STEP), mn = SymbolInfoDouble(Sym, SYMBOL_VOLUME_MIN);
   if(st <= 0) st = 0.01;
   double h = MathRound(vol / 2 / st) * st;
   if(h < mn || vol - h < mn) return 0;
   return NormalizeDouble(h, 2);
}

string DoHalf(ulong tk)
{
   int n = 0, skip = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(!Mine(t) || (tk > 0 && t != tk)) continue;
      double h = HalfVol(PositionGetDouble(POSITION_VOLUME));
      if(h <= 0) { skip++; continue; }
      if(trade.PositionClosePartial(t, h) && OkRet()) n++; else return "0|" + Rej();
   }
   if(n == 0) return skip > 0 ? "0|Lệnh quá nhỏ để chốt 50% (cần từ 0.02 lot)." : "0|Không có lệnh mở.";
   return "1|Đã chốt 50% " + IntegerToString(n) + " lệnh." + (skip > 0 ? " Bỏ qua " + IntegerToString(skip) + " lệnh quá nhỏ." : "");
}

string DoCancel(ulong tk)
{
   int n = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong t = OrderGetTicket(i);
      if(OrderGetInteger(ORDER_MAGIC) != EA_MAGIC || OrderGetString(ORDER_SYMBOL) != Sym || (tk > 0 && t != tk)) continue;
      if(trade.OrderDelete(t) && OkRet()) n++; else return "0|" + Rej();
   }
   return n > 0 ? "1|Đã hủy " + IntegerToString(n) + " lệnh chờ." : "0|Không có lệnh chờ.";
}

string DoBe(ulong tk)
{
   double u = Unit(), mind = MinDist(); int dg = Dg(), n = 0, skip = 0;
   MqlTick t; SymbolInfoTick(Sym, t);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong k = PositionGetTicket(i);
      if(!Mine(k) || (tk > 0 && k != tk)) continue;
      bool buy = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
      double op = PositionGetDouble(POSITION_PRICE_OPEN), px = buy ? t.bid : t.ask;
      double s = NormalizeDouble(buy ? op + gBeLock * u : op - gBeLock * u, dg);
      if((buy && px - s < mind) || (!buy && s - px < mind)) { skip++; continue; }
      if(trade.PositionModify(k, s, PositionGetDouble(POSITION_TP)) && OkRet()) n++; else return "0|" + Rej();
   }
   if(n == 0) return skip > 0 ? "0|Lệnh chưa lãi đủ để khóa hòa vốn." : "0|Không có lệnh mở.";
   return "1|Đã dời SL hòa vốn " + IntegerToString(n) + " lệnh.";
}

// Copy lệnh bot (chế độ "tài khoản vào lệnh"): chốt 1 phần theo tỉ lệ tổng khối lượng EA, lệnh cũ trước
string DoPart(double frac)
{
   if(frac <= 0 || frac > 1) return "0|Tỉ lệ chốt không hợp lệ.";
   double st = SymbolInfoDouble(Sym, SYMBOL_VOLUME_STEP), mn = SymbolInfoDouble(Sym, SYMBOL_VOLUME_MIN);
   if(st <= 0) st = 0.01;
   double tot = 0; ulong tk[]; int m = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong t = PositionGetTicket(i);
      if(!Mine(t)) continue;
      tot += PositionGetDouble(POSITION_VOLUME); ArrayResize(tk, m + 1); tk[m++] = t;
   }
   if(m == 0) return "0|Không có lệnh mở.";
   ArraySort(tk);
   double want = MathFloor(tot * frac / st + 1e-9) * st, left = want, done = 0;
   if(want < mn - 1e-9) return "0|Khối lượng quá nhỏ để chốt " + D(frac * 100, 0) + "%.";
   for(int i = 0; i < m && left >= mn - 1e-9; i++)
   {
      if(!PositionSelectByTicket(tk[i])) continue;
      double v = PositionGetDouble(POSITION_VOLUME), c = MathMin(v, left);
      if(v - c > 1e-9 && v - c < mn - 1e-9) c = v;               // phần còn lại dưới lot tối thiểu: đóng hết lệnh
      c = NormalizeDouble(MathFloor(c / st + 1e-9) * st, 2);
      if(c < mn - 1e-9) continue;
      bool ok = c >= v - 1e-9 ? trade.PositionClose(tk[i]) : trade.PositionClosePartial(tk[i], c);
      if(!ok || !OkRet()) return "0|" + Rej();
      left -= c; done += c;
   }
   return "1|Đã chốt " + D(done, 2) + " / " + D(tot, 2) + " lot.";
}

// Dời SL mọi lệnh EA về giá vào trung bình (+ cộng thêm off theo chiều có lợi)
string DoBeAvg(double off)
{
   double v = 0, pv = 0; int dir = 0, dg = Dg(); double mind = MinDist();
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong t = PositionGetTicket(i);
      if(!Mine(t)) continue;
      double vol = PositionGetDouble(POSITION_VOLUME);
      v += vol; pv += vol * PositionGetDouble(POSITION_PRICE_OPEN);
      dir = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
   }
   if(v <= 0) return "0|Không có lệnh mở.";
   double be = NormalizeDouble(pv / v + dir * off * Unit(), dg);
   MqlTick t; SymbolInfoTick(Sym, t);
   double px = dir > 0 ? t.bid : t.ask;
   if(dir * (px - be) < mind) return "0|Giá chưa đủ xa giá trung bình để dời SL hòa vốn.";
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong k = PositionGetTicket(i);
      if(!Mine(k)) continue;
      double sl = PositionGetDouble(POSITION_SL);
      if(sl != 0 && dir * (be - sl) <= 0) continue;              // SL hiện tại đã tốt hơn
      if(trade.PositionModify(k, be, PositionGetDouble(POSITION_TP)) && OkRet()) n++; else return "0|" + Rej();
   }
   return "1|Đã dời SL " + IntegerToString(n) + " lệnh về giá TB " + D(be, dg) + ".";
}

// ---- TP1 đặt sẵn trên sàn ----
void Tp1Watch0();
bool IsTp1(ulong t) { return PositionSelectByTicket(t) && StringFind(PositionGetString(POSITION_COMMENT), "TP1") >= 0; }

bool AvgDir(double &avg, int &dir, int &n, int &n1)
{
   double v = 0, pv = 0; n = 0; n1 = 0; dir = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong t = PositionGetTicket(i);
      if(!Mine(t)) continue;
      double vol = PositionGetDouble(POSITION_VOLUME);
      v += vol; pv += vol * PositionGetDouble(POSITION_PRICE_OPEN); n++;
      dir = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
      if(StringFind(PositionGetString(POSITION_COMMENT), "TP1") >= 0) n1++;
   }
   avg = v > 0 ? pv / v : 0;
   return n > 0;
}

// TP của các lệnh "PinYin TP1" = giá vào TB của EA ± khoảng TP1 (khoảng cách lấy từ tài khoản tín hiệu)
void SetTp1()
{
   double avg; int dir, n, n1;
   if(gTp1Dist <= 0 || gTp1Done || !AvgDir(avg, dir, n, n1) || n1 == 0) return;
   int dg = Dg(); double mind = MinDist(), pt = SymbolInfoDouble(Sym, SYMBOL_POINT);
   double tp = NormalizeDouble(avg + dir * gTp1Dist * Unit(), dg);
   MqlTick t; if(!SymbolInfoTick(Sym, t) || t.bid <= 0) return;
   double px = dir > 0 ? t.bid : t.ask;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong k = PositionGetTicket(i);
      if(!Mine(k) || !IsTp1(k)) continue;
      if(dir * (tp - px) < mind) { trade.PositionClose(k); continue; }       // giá đã vượt TP1: chốt luôn
      if(MathAbs(PositionGetDouble(POSITION_TP) - tp) > pt / 2) trade.PositionModify(k, PositionGetDouble(POSITION_SL), tp);
   }
   gTp1Applied = gTp1Dist;
}

string BeAt(double be, int dir)
{
   int dg = Dg(), n = 0; double mind = MinDist(); be = NormalizeDouble(be, dg);
   MqlTick t; SymbolInfoTick(Sym, t);
   double px = dir > 0 ? t.bid : t.ask;
   if(dir * (px - be) < mind) return "0|Giá chưa đủ xa giá trung bình để dời SL hòa vốn.";
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong k = PositionGetTicket(i);
      if(!Mine(k)) continue;
      double sl = PositionGetDouble(POSITION_SL);
      if(sl != 0 && dir * (be - sl) <= 0) continue;
      if(trade.PositionModify(k, be, PositionGetDouble(POSITION_TP)) && OkRet()) n++;
   }
   return "1|Đã dời SL " + IntegerToString(n) + " lệnh về hòa vốn " + D(be, dg) + ".";
}

// Theo dõi mỗi giây: sàn đã chốt phần TP1 → EA tự dời SL phần còn lại về giá vào TB (không cần máy chủ)
void Tp1Watch()
{
   string o = gTag; gTag = gTp1Tag;
   gScope = AnyBot();
   Tp1Watch0();
   gScope = false; gTag = o;
}

void Tp1Watch0()
{
   double avg; int dir, n, n1;
   if(!AvgDir(avg, dir, n, n1)) { gHadTp1 = false; gTp1Done = false; gSplit = false; gTp1Applied = -1; gNPos = 0; return; }
   if(n1 > 0 && !gTp1Done)
   {
      gHadTp1 = true; gAvg0 = avg;
      if(n != gNPos || MathAbs(gTp1Applied - gTp1Dist) >= 0.01) SetTp1();
   }
   gNPos = n;
   if(gHadTp1 && n1 == 0 && !gTp1Done)
   {
      gTp1Done = true;
      string r = gBeOnTp1 ? BeAt(gAvg0 + dir * gBeOff * Unit(), dir) : "1|không dời hòa vốn (đang tắt)";
      results += "N|Sàn đã chốt TP1 → " + Clean(StringSubstr(r, 2)) + "\n";
   }
}

// Vào lệnh copy: tách pct% khối lượng thành lệnh "PinYin TP1" có TP đặt sẵn trên sàn
string DoDOrd(string side, double lot, double sl, double pct)
{
   bool buy = side == "BUY"; double u = Unit(), mind = MinDist(); int dg = Dg();
   double st = SymbolInfoDouble(Sym, SYMBOL_VOLUME_STEP), mn = SymbolInfoDouble(Sym, SYMBOL_VOLUME_MIN);
   if(st <= 0) st = 0.01;
   lot = NormLot(lot);
   if(lot <= 0) return "0|Lot không hợp lệ với sàn.";
   MqlTick t; if(!SymbolInfoTick(Sym, t) || t.bid <= 0) return "0|Chưa có giá (thị trường có thể đang đóng).";
   double avg; int dir, n, n1;
   if(!AvgDir(avg, dir, n, n1)) { gTp1Done = false; gHadTp1 = false; gSplit = false; gTp1Applied = -1; }
   double mkt = buy ? t.ask : t.bid, s = 0;
   if(sl > 0) { double d = MathMax(sl * u, mind); s = NormalizeDouble(buy ? mkt - d : mkt + d, dg); }
   double la = (gTp1Dist > 0 && !gTp1Done && pct > 0) ? NormalizeDouble(MathFloor(lot * pct / 100 / st + 1e-9) * st, 2) : 0;
   if(la >= lot - 1e-9) la = lot;
   double lb = NormalizeDouble(lot - la, 2);
   if(la < mn - 1e-9 || (lb > 1e-9 && lb < mn - 1e-9)) { la = 0; lb = lot; }   // không tách được: máy chủ chốt TP1 như cũ
   double p = la > 0 ? NormalizeDouble(buy ? mkt + MathMax(gTp1Dist * u, mind) : mkt - MathMax(gTp1Dist * u, mind), dg) : 0;
   string msg = "";
   if(la > 0)
   {
      bool ok = buy ? trade.Buy(la, Sym, 0, s, p, CmT(true)) : trade.Sell(la, Sym, 0, s, p, CmT(true));
      if(!ok || !OkRet()) return "0|" + Rej();
      gSplit = true;
      msg = D(la, 2) + " lot (TP1 " + D(p, dg) + " đặt sẵn trên sàn)";
   }
   if(lb > 0)
   {
      bool ok = buy ? trade.Buy(lb, Sym, 0, s, 0, CmT(false)) : trade.Sell(lb, Sym, 0, s, 0, CmT(false));
      if(!ok || !OkRet()) return (la > 0 ? "1|Đã vào " + msg + " nhưng phần còn lại lỗi: " : "0|") + Rej();
      msg += (msg != "" ? " + " : "") + D(lb, 2) + " lot";
   }
   gNPos = -1;                                        // vòng sau tính lại TP1 theo giá vào TB
   return "1|Đã vào " + side + " " + msg + " @ " + D(trade.ResultPrice(), dg) + (s > 0 ? " · SL " + D(s, dg) : "");
}

// Máy chủ báo chốt TP1: đóng phần "PinYin TP1" (nếu sàn chưa chốt), không tách được thì chốt theo tỉ lệ
string DoTp1Part(double frac)
{
   double avg; int dir, n, n1;
   if(!AvgDir(avg, dir, n, n1)) return "0|Không có lệnh mở.";
   if(n1 > 0)
   {
      int k = 0;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong t = PositionGetTicket(i);
         if(Mine(t) && IsTp1(t)) { if(trade.PositionClose(t) && OkRet()) k++; else return "0|" + Rej(); }
      }
      return "1|Đã chốt phần TP1 (" + IntegerToString(k) + " lệnh).";
   }
   if(gTp1Done || gHadTp1) return "1|Sàn đã chốt TP1 trước đó.";
   gTp1Done = true;
   return DoPart(frac);
}

bool Done(string id) { for(int i = 0; i < doneN; i++) if(doneIds[i] == id) return true; return false; }
void MarkDone(string id) { if(doneN < 50) doneIds[doneN++] = id; else { for(int i = 1; i < 50; i++) doneIds[i - 1] = doneIds[i]; doneIds[49] = id; } }

void Exec(string &f[])
{
   int n = ArraySize(f); if(n < 3) return;
   string id = f[1], c = f[2], r;
   gScope = false; gTag = "";
   while(n > 3)
   {
      string x = f[n - 1];
      if(x == "B") { gScope = true; n--; }
      else if(StringFind(x, "T:") == 0) { gTag = StringSubstr(x, 2); n--; }
      else break;
   }
   if(Done(id)) return;
   MarkDone(id);
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED) || !TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) r = "0|MT5 đang tắt nút Algo Trading / EA chưa được phép giao dịch.";
   else if(c == "ORDER" && n >= 10) r = DoOrder(f[3], StringToDouble(f[4]), f[5], StringToDouble(f[6]), StringToDouble(f[7]), StringToDouble(f[8]), f[9]);
   else if(c == "CLOSE")  r = DoClose((ulong)StringToInteger(n > 3 ? f[3] : "0"));
   else if(c == "HALF")   r = DoHalf((ulong)StringToInteger(n > 3 ? f[3] : "0"));
   else if(c == "CANCEL") r = DoCancel((ulong)StringToInteger(n > 3 ? f[3] : "0"));
   else if(c == "BE")     r = DoBe((ulong)StringToInteger(n > 3 ? f[3] : "0"));
   else if(c == "PART" && n > 3)  r = DoPart(StringToDouble(f[3]));
   else if(c == "BEAVG")  r = DoBeAvg(n > 3 ? StringToDouble(f[3]) : 0);
   else if(c == "DORD" && n >= 7) r = DoDOrd(f[3], StringToDouble(f[4]), StringToDouble(f[5]), StringToDouble(f[6]));
   else if(c == "TP1PART" && n > 3) r = DoTp1Part(StringToDouble(f[3]));
   else r = "0|Lệnh không hỗ trợ: " + c;
   gScope = false; gTag = "";
   results += "R|" + id + "|" + StringSubstr(r, 0, 1) + "|" + Clean(StringSubstr(r, 2)) + "\n";   // r = "1|thông báo"
}

//---------------- Lá chắn (chạy ngay trên MT5 của khách) ----------------
bool Guarded(ulong t)
{
   if(!PositionSelectByTicket(t) || PositionGetString(POSITION_SYMBOL) != Sym) return false;
   long mg = PositionGetInteger(POSITION_MAGIC);
   if(StringFind(PositionGetString(POSITION_COMMENT), "PinYin AUTO") == 0 || IsBotC()) return false;   // lệnh bot PinYin: bot tự quản
   return mg == EA_MAGIC || (gMan && mg == 0);
}

void AutoSlTp(ulong k)
{
   string gv = "PY_S_" + IntegerToString((long)k);       // giá trị = số lần đã thử (xong thì đặt 99)
   double tries = GlobalVariableCheck(gv) ? GlobalVariableGet(gv) : 0;
   if(tries >= 5) return;
   double sl = PositionGetDouble(POSITION_SL), tp = PositionGetDouble(POSITION_TP);
   if(sl > 0 && tp > 0) { GlobalVariableSet(gv, 99); return; }
   GlobalVariableSet(gv, tries + 1);
   bool buy = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
   double op = PositionGetDouble(POSITION_PRICE_OPEN), u = Unit(), mind = MinDist(); int dg = Dg();
   MqlTick t; if(!SymbolInfoTick(Sym, t) || t.bid <= 0) return;
   double px = buy ? t.bid : t.ask, ns = sl, nt = tp;
   if(sl == 0 && gSl > 0) { ns = NormalizeDouble(buy ? op - gSl * u : op + gSl * u, dg); if((buy && px - ns < mind) || (!buy && ns - px < mind)) ns = NormalizeDouble(buy ? px - mind : px + mind, dg); }
   if(tp == 0 && gTp > 0) { nt = NormalizeDouble(buy ? op + gTp * u : op - gTp * u, dg); if((buy && nt - px < mind) || (!buy && px - nt < mind)) nt = tp; }
   if(ns == sl && nt == tp) { GlobalVariableSet(gv, 99); return; }
   if(trade.PositionModify(k, ns, nt) && OkRet()) GlobalVariableSet(gv, 99);     // lỗi thì giây sau thử lại (tối đa 5 lần)
}

void Guard()
{
   if(!(gBe || gTr || gMan)) return;
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED) || !TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) return;
   double u = Unit(), mind = MinDist(), pt = SymbolInfoDouble(Sym, SYMBOL_POINT); int dg = Dg();
   MqlTick t; if(!SymbolInfoTick(Sym, t) || t.bid <= 0) return;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong k = PositionGetTicket(i);
      if(!Guarded(k)) continue;
      if(PositionGetInteger(POSITION_MAGIC) == 0 && gAuto) { AutoSlTp(k); if(!PositionSelectByTicket(k)) continue; }
      if(!(gBe || gTr)) continue;
      bool buy = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
      double op = PositionGetDouble(POSITION_PRICE_OPEN), sl = PositionGetDouble(POSITION_SL), tp = PositionGetDouble(POSITION_TP);
      double px = buy ? t.bid : t.ask, gain = buy ? (px - op) / u : (op - px) / u;
      if(gTr && gain >= gTrAt)
      {
         string gv = "PY_H_" + IntegerToString((long)k);
         if(gHalf && !GlobalVariableCheck(gv))
         {
            GlobalVariableSet(gv, 1);
            double h = HalfVol(PositionGetDouble(POSITION_VOLUME));
            if(h > 0) trade.PositionClosePartial(k, h);
            if(!PositionSelectByTicket(k)) continue;
         }
         double ns = NormalizeDouble(buy ? px - gTrStep * u : px + gTrStep * u, dg);
         bool better = buy ? ns > sl + pt / 2 : (sl == 0 || ns < sl - pt / 2);
         bool room = buy ? px - ns >= mind : ns - px >= mind;
         if(better && room) trade.PositionModify(k, ns, tp);
         continue;
      }
      if(gBe && gain >= gBeAt)
      {
         double ns = NormalizeDouble(buy ? op + gBeLock * u : op - gBeLock * u, dg);
         bool better = buy ? ns > sl + pt / 2 : (sl == 0 || ns < sl - pt / 2);
         bool room = buy ? px - ns >= mind : ns - px >= mind;
         if(better && room) trade.PositionModify(k, ns, tp);
      }
   }
}

//---------------- Đồng bộ với máy chủ PinYin ----------------
string Body()
{
   int dg = Dg();
   string b = "K|" + InpKey + "\nV|" + EA_VER + "\n";
   b += "A|" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "|" + Clean(AccountInfoString(ACCOUNT_SERVER)) + "|" + Clean(AccountInfoString(ACCOUNT_COMPANY))
        + "|" + AccountInfoString(ACCOUNT_CURRENCY) + "|" + D(AccountInfoDouble(ACCOUNT_BALANCE), 2) + "|" + D(AccountInfoDouble(ACCOUNT_EQUITY), 2)
        + "|" + D(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + "|" + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE))
        + "|" + (AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO ? "1" : "0")
        + "|" + ((MQLInfoInteger(MQL_TRADE_ALLOWED) && TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) ? "1" : "0")
        + "|" + Clean(AccountInfoString(ACCOUNT_NAME)) + "\n";
   MqlTick t; SymbolInfoTick(Sym, t);
   b += "S|" + Sym + "|" + D(t.bid, dg) + "|" + D(t.ask, dg) + "|" + IntegerToString(dg) + "|" + D(SymbolInfoDouble(Sym, SYMBOL_POINT), 8)
        + "|" + D(MinDist(), dg) + "|" + D(SymbolInfoDouble(Sym, SYMBOL_VOLUME_MIN), 2) + "|" + D(SymbolInfoDouble(Sym, SYMBOL_VOLUME_STEP), 2) + "|" + D(Unit(), 8) + "\n";
   for(int i = 0; i < 3; i++)
   {
      double hi = 0, lo = 0; HiLo(i, hi, lo);
      b += "I|" + TFN[i] + "|" + D(Ema(i), dg) + "|" + D(hi, dg) + "|" + D(lo, dg) + "\n";
   }
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong k = PositionGetTicket(i);
      if(!PositionSelectByTicket(k) || PositionGetInteger(POSITION_MAGIC) != EA_MAGIC || PositionGetString(POSITION_SYMBOL) != Sym) continue;
      b += "P|" + IntegerToString((long)k) + "|" + (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL")
           + "|" + D(PositionGetDouble(POSITION_VOLUME), 2) + "|" + D(PositionGetDouble(POSITION_PRICE_OPEN), dg)
           + "|" + D(PositionGetDouble(POSITION_SL), dg) + "|" + D(PositionGetDouble(POSITION_TP), dg)
           + "|" + D(PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP), 2)
           + "|" + (GlobalVariableCheck("PY_H_" + IntegerToString((long)k)) ? "1" : "0") + "|" + (IsBotC() ? "1" : "0") + "|" + TagOf() + "\n";
   }
   for(int i = 0; i < OrdersTotal(); i++)
   {
      ulong k = OrderGetTicket(i);
      if(OrderGetInteger(ORDER_MAGIC) != EA_MAGIC || OrderGetString(ORDER_SYMBOL) != Sym) continue;
      long ty = OrderGetInteger(ORDER_TYPE);
      string tn = ty == ORDER_TYPE_BUY_LIMIT ? "BUY LIMIT" : ty == ORDER_TYPE_SELL_LIMIT ? "SELL LIMIT" : ty == ORDER_TYPE_BUY_STOP ? "BUY STOP" : ty == ORDER_TYPE_SELL_STOP ? "SELL STOP" : "KHÁC";
      b += "O|" + IntegerToString((long)k) + "|" + tn + "|" + D(OrderGetDouble(ORDER_VOLUME_CURRENT), 2) + "|" + D(OrderGetDouble(ORDER_PRICE_OPEN), dg)
           + "|" + D(OrderGetDouble(ORDER_SL), dg) + "|" + D(OrderGetDouble(ORDER_TP), dg) + "\n";
   }
   b += results;
   return b;
}

void Sync()
{
   string body = Body();
   char data[], res[]; string rh;
   StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   int sz = ArraySize(data); if(sz > 0 && data[sz - 1] == 0) ArrayResize(data, sz - 1);
   ResetLastError();
   int code = WebRequest("POST", InpUrl + "/ea/sync", "Content-Type: text/plain; charset=utf-8\r\n", 5000, data, res, rh);
   if(code == -1)
   {
      int e = GetLastError();
      gStatus = e == 4014 ? "❌ Chưa cho phép WebRequest. Vào Tools > Options > Expert Advisors > tích 'Allow WebRequest' và thêm " + InpUrl
                         : "❌ Không kết nối được PinYin (lỗi " + IntegerToString(e) + "). Kiểm tra mạng.";
      return;
   }
   if(code != 200) { gStatus = "❌ PinYin trả lỗi " + IntegerToString(code); return; }
   results = "";                                     // máy chủ đã nhận kết quả
   string txt = CharArrayToString(res, 0, WHOLE_ARRAY, CP_UTF8), lines[];
   int n = StringSplit(txt, '\n', lines);
   gStatus = "✅ Đã kết nối PinYin · #" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + " · " + Sym;
   for(int i = 0; i < n; i++)
   {
      string f[]; int m = StringSplit(lines[i], '|', f);
      if(m < 1) continue;
      if(f[0] == "X") { gStatus = "⛔ " + (m > 1 ? f[1] : "PinYin từ chối kết nối"); return; }
      if(f[0] == "G" && m >= 8)
      {
         gBe = f[1] == "1"; gBeAt = StringToDouble(f[2]); gBeLock = StringToDouble(f[3]);
         gTr = f[4] == "1"; gTrAt = StringToDouble(f[5]); gTrStep = StringToDouble(f[6]); gHalf = f[7] == "1";
         if(m >= 12) { gMan = f[8] == "1"; gAuto = f[9] == "1"; gSl = StringToDouble(f[10]); gTp = StringToDouble(f[11]); }
      }
      if(f[0] == "Q" && m >= 4)
      {
         string tg = m >= 5 ? f[4] : "";
         if(tg != gTp1Tag) { gTp1Done = false; gHadTp1 = false; gSplit = false; gTp1Applied = -1; gNPos = 0; }   // đổi bot DCA nguồn
         gTp1Dist = StringToDouble(f[1]); gBeOnTp1 = f[2] == "1"; gBeOff = StringToDouble(f[3]); gTp1Tag = tg;
      }
      if(f[0] == "C") Exec(f);
   }
}

//---------------- Vòng đời EA ----------------
int OnInit()
{
   if(StringLen(InpKey) < 10) { Alert("PinYin: chưa nhập Mã kết nối (lấy trên trang pinyin.id.vn)."); return INIT_PARAMETERS_INCORRECT; }
   Sym = InpSymbol != "" ? InpSymbol : FindSym("XAUUSD");
   if(Sym == "" || !SymbolSelect(Sym, true)) { Alert("PinYin: không tìm thấy mã vàng XAUUSD trên sàn. Nhập tên vào ô InpSymbol."); return INIT_FAILED; }
   for(int i = 0; i < 3; i++) hEma[i] = iMA(Sym, TFS[i], 9, 0, MODE_EMA, PRICE_CLOSE);
   trade.SetExpertMagicNumber(EA_MAGIC);
   trade.SetDeviationInPoints(30);
   trade.SetTypeFillingBySymbol(Sym);
   EventSetMillisecondTimer(1000);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); for(int i = 0; i < 3; i++) IndicatorRelease(hEma[i]); Comment(""); }

void OnTimer()
{
   Guard();
   Tp1Watch();
   Sync();
   Comment("PinYin Bridge " + EA_VER + "\n" + gStatus + "\n" + TimeToString(TimeLocal(), TIME_SECONDS));
}

void OnTick() {}

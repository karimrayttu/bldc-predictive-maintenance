/* BLDC PHM data node: NUCLEO-F401RE, BLD-510B driver, 42BL41.010 motor,
 * KX134-1211 accelerometer, 10k NTC.
 *
 * Pin map and wiring        docs/PINOUT.md
 * Stream format, 44 columns bldc_phm/schema.py
 * Design and rationale      docs/GUIDE.md
 *
 * 44 CSV columns at 10 Hz, classified on board twice a second.
 * HSI 16 MHz, TIM2 = 1 us timebase, 8-pole motor = 24 Hall edges/rev.
 */
#include <stdint.h>
#include "speeds_gen.h"
#include "current_cal.h"
#include "fault_tree.h"

/* Build guard: this tree takes raw mV/mg/permil, not physical units */
_Static_assert(FAULT_TREE_FEATURE_SET == FAULT_TREE_FEATURE_SET_MCU_PROXY_RAW,
               "fault_tree.c does not take main.c's raw mV/mg feature vector");

/* On-device detector: depth-7 tree, 33 nodes, 1 s window every 0.5 s.
 * Provenance and the gravity leak: fault_tree.c and docs/GUIDE.md.
 * Classifies only while the motor turns; 15 = idle. */
#define FAULT_IDLE_NA  15
#define FT_WIN 10
static float ft_ring[FT_WIN][10];
static int ft_n = 0, ft_head = 0;
static float ft_anchor_mv = 1650.0f;
static uint32_t ft_anchor_settle = 0;
#define FT_G0X (-12.0f)
#define FT_G0Y (-21.0f)
#define FT_G0Z (1050.0f)

/* Demo mount reads 1571 mg per g against the campaign's 1050, so classifier
 * inputs are mapped into the trained frame. Streamed columns stay raw. */
#define FT_GBX (-19.0f)
#define FT_GBY (-12.0f)
#define FT_GBZ (1577.0f)
#define FT_KG  (0.6658f)
/* re-stamped 2026-07-31 after overnight accel-offset drift; 15-s rest median */

/* Debounce: a fault needs 4 consecutive windows, healthy needs 2 */
static uint32_t ft_cand = FAULT_IDLE_NA;
static uint32_t ft_streak = 0;

/* Temperature is the authoritative overheat signature, so it overrides the tree */
static float ft_tbase_mv = 0.0f;      /* 0 = not yet learned */
static float ft_gprev = -1.0f;        /* last window's gdev (loose stability) */
static uint32_t ft_since_drag = 9999u; /* classify ticks since DRAG was reported */

static float ft_sqrtf(float r)
{
    float g;
    int it;
    if (r < 1e-9f) return 0.0f;
    g = r * 0.5f;
    for (it = 0; it < 8; it++) g = 0.5f * (g + r / g);
    return g;
}

/* RCC */
#define RCC_AHB1ENR   (*(volatile uint32_t *)0x40023830u)
#define RCC_APB1ENR   (*(volatile uint32_t *)0x40023840u)
#define RCC_APB2ENR   (*(volatile uint32_t *)0x40023844u) /* ADC1 b8, SYSCFG b14 */

/* GPIO */
#define GPIOA_MODER   (*(volatile uint32_t *)0x40020000u)
#define GPIOA_OTYPER  (*(volatile uint32_t *)0x40020004u)
#define GPIOA_PUPDR   (*(volatile uint32_t *)0x4002000Cu)
#define GPIOA_IDR     (*(volatile uint32_t *)0x40020010u)
#define GPIOA_BSRR    (*(volatile uint32_t *)0x40020018u)
#define GPIOA_AFRL    (*(volatile uint32_t *)0x40020020u)
#define GPIOB_MODER   (*(volatile uint32_t *)0x40020400u)
#define GPIOB_OTYPER  (*(volatile uint32_t *)0x40020404u)
#define GPIOB_PUPDR   (*(volatile uint32_t *)0x4002040Cu)
#define GPIOB_IDR     (*(volatile uint32_t *)0x40020410u)
#define GPIOB_BSRR    (*(volatile uint32_t *)0x40020418u)
#define GPIOB_AFRL    (*(volatile uint32_t *)0x40020420u)
#define GPIOC_MODER   (*(volatile uint32_t *)0x40020800u)
#define GPIOC_PUPDR   (*(volatile uint32_t *)0x4002080Cu)
#define GPIOC_IDR     (*(volatile uint32_t *)0x40020810u)

/* TIM2 (1 us timebase) / TIM3 (PWM) */
#define TIM2_CR1      (*(volatile uint32_t *)0x40000000u)
#define TIM2_EGR      (*(volatile uint32_t *)0x40000014u)
#define TIM2_CNT      (*(volatile uint32_t *)0x40000024u)
#define TIM2_PSC      (*(volatile uint32_t *)0x40000028u)
#define TIM2_ARR      (*(volatile uint32_t *)0x4000002Cu)
#define TIM3_CR1      (*(volatile uint32_t *)0x40000400u)
#define TIM3_EGR      (*(volatile uint32_t *)0x40000414u)
#define TIM3_CCMR1    (*(volatile uint32_t *)0x40000418u)
#define TIM3_CCER     (*(volatile uint32_t *)0x40000420u)
#define TIM3_PSC      (*(volatile uint32_t *)0x40000428u)
#define TIM3_ARR      (*(volatile uint32_t *)0x4000042Cu)
#define TIM3_CCR1     (*(volatile uint32_t *)0x40000434u)

/* USART1 (PA9/D8 -> Raspberry Pi RX, TX-only telemetry) */
#define USART1_SR     (*(volatile uint32_t *)0x40011000u)
#define USART1_DR     (*(volatile uint32_t *)0x40011004u)
#define USART1_BRR    (*(volatile uint32_t *)0x40011008u)
#define USART1_CR1    (*(volatile uint32_t *)0x4001100Cu)
#define GPIOA_AFRH    (*(volatile uint32_t *)0x40020024u)

/* USART2 */
#define USART2_SR     (*(volatile uint32_t *)0x40004400u)
#define USART2_DR     (*(volatile uint32_t *)0x40004404u)
#define USART2_BRR    (*(volatile uint32_t *)0x40004408u)
#define USART2_CR1    (*(volatile uint32_t *)0x4000440Cu)

/* ADC1 */
#define ADC1_SR       (*(volatile uint32_t *)0x40012000u)
#define ADC1_CR1      (*(volatile uint32_t *)0x40012004u)
#define ADC1_CR2      (*(volatile uint32_t *)0x40012008u)
#define ADC1_SMPR1    (*(volatile uint32_t *)0x4001200Cu)
#define ADC1_SMPR2    (*(volatile uint32_t *)0x40012010u)
#define ADC1_SQR1     (*(volatile uint32_t *)0x4001202Cu)
#define ADC1_SQR3     (*(volatile uint32_t *)0x40012034u)
#define ADC1_DR       (*(volatile uint32_t *)0x4001204Cu)
#define ADC_CCR       (*(volatile uint32_t *)0x40012304u)

/* EXTI / SYSCFG / NVIC */
#define SYSCFG_EXTICR1 (*(volatile uint32_t *)0x40013808u) /* EXTI0-3   */
#define SYSCFG_EXTICR2 (*(volatile uint32_t *)0x4001380Cu) /* EXTI4-7   */
#define SYSCFG_EXTICR3 (*(volatile uint32_t *)0x40013810u) /* EXTI8-11  */
#define EXTI_IMR      (*(volatile uint32_t *)0x40013C00u)
#define EXTI_RTSR     (*(volatile uint32_t *)0x40013C08u)
#define EXTI_FTSR     (*(volatile uint32_t *)0x40013C0Cu)
#define EXTI_PR       (*(volatile uint32_t *)0x40013C14u)
#define NVIC_ISER0    (*(volatile uint32_t *)0xE000E100u)
#define NVIC_ISER1    (*(volatile uint32_t *)0xE000E104u)

#define CH_CURRENT    0u    /* PA0 */
#define CH_TEMP       1u    /* PA1 */
#define CH_PC0        10u   /* PC0 */
#define CH_VREFINT    17u

#define PWM_PERIOD_TICKS 1000u
#define KX_COUNTS_PER_G  2048   /* +/-16 g */

/* Per-channel self-verification, streamed as sensor_status. Each channel is
 * checked against a physical invariant that a broken wire cannot fake:
 *   ADC    VREFINT is a fixed internal 1.21 V bandgap
 *   CURR   INA240 idles at its ~1.65 V REF; an open or short pins a rail
 *   TEMP   NTC divider must sit between the rails
 *   ACCEL  |(x,y,z)| reads ~1 g in any orientation
 *   HALL   all six legal codes appear within one revolution */
#define ST_ADC_OK    (1u << 0)
#define ST_CURR_OK   (1u << 1)
#define ST_TEMP_OK   (1u << 2)
#define ST_ACCEL_OK  (1u << 3)
#define ST_HALL_OK   (1u << 4)
#define ST_SPIN      (1u << 5)
#define ST_KX_ID_OK  (1u << 6)
#define ST_HALL_ALL6 (1u << 7)
#define ST_HALL_SEEN_SHIFT 8    /* bits 8..13: Hall codes 1..6 seen since boot */

/* plausibility bands */
#define VREF_RAW_MIN 1350u      /* 1.21 V nominal -> ~1502 counts             */
#define VREF_RAW_MAX 1650u
#define CURR_MV_MIN   100u      /* below/above => INA240 railed = open/short  */
#define CURR_MV_MAX  3200u
#define TEMP_MV_MIN   150u      /* below => NTC shorted; above => open        */
#define TEMP_MV_MAX  3150u
#define ACC_1G_MIN   1400u      /* 0.68 g, generous, allows heavy vibration */
#define ACC_1G_MAX   8192u      /* 4 g: drag vibration pushed the block mean
                                 * past the old 1.37 g. A dead sensor reads ~0,
                                 * a mis-scaled one pins at 16 g. */

    /* median of three: drops isolated spikes, passes real load steps */
static inline uint32_t med3(uint32_t a, uint32_t b, uint32_t c)
{
    if (a > b) { uint32_t t = a; a = b; b = t; }
    if (b > c) { b = c; }
    return (a > b) ? a : b;
}

volatile uint32_t g_tlm[24];
enum { T_RPM=0,T_AVG_US,T_MIN_US,T_MAX_US,T_RIPPLE,T_EDGES,T_STATE,
       T_ILLEGAL,T_COAST_MS,T_RPM_ATCUT,T_SPEEDIDX,
       T_PSTD,T_MECH1X,T_ELEC,T_KXADDR,T_KXWHO,T_TEMPMV,T_IDCMV,T_STATUS };

/* Hall edge ring (ISR producer, main consumer) */
volatile uint32_t hall_t[64];
volatile uint8_t  hall_s[64];
volatile uint32_t hall_head = 0u;      /* ISR increments */

/* UART RX ring (ISR producer, main consumer) */
volatile uint8_t  rx_ring[64];
volatile uint32_t rx_head = 0u;

static inline uint32_t now_us(void) { return TIM2_CNT; }
/* hall state: bit0=PA10(D2), bit1=PB3(D3), bit2=PB5(D4) */
static inline uint32_t hall_state_now(void)
{
    uint32_t a = GPIOA_IDR, b = GPIOB_IDR;
    return ((a >> 10) & 1u) | (((b >> 3) & 1u) << 1) | (((b >> 5) & 1u) << 2);
}
static inline uint32_t button(void) { return ((GPIOC_IDR >> 13) & 1u) ? 0u : 1u; }

static void hall_capture(void)
{
    uint32_t h = hall_head;
    hall_t[h & 63u] = now_us();
    hall_s[h & 63u] = (uint8_t)hall_state_now();
    hall_head = h + 1u;
}

void EXTI3_IRQHandler(void)      { EXTI_PR = (1u << 3);  hall_capture(); }
void EXTI9_5_IRQHandler(void)    { EXTI_PR = (1u << 5);  hall_capture(); }
void EXTI15_10_IRQHandler(void)  { EXTI_PR = (1u << 10); hall_capture(); }
void USART2_IRQHandler(void)
{
    uint32_t sr = USART2_SR;
    if (sr & ((1u << 5) | (1u << 3))) {            /* RXNE or ORE */
        uint8_t d = (uint8_t)USART2_DR;            /* read clears  */
        if (sr & (1u << 5)) { rx_ring[rx_head & 63u] = d; rx_head = rx_head + 1u; }
        g_tlm[18] = g_tlm[18] + 1u;                /* RX byte diag counter */
    }
}

/* misc helpers */
static const short C1[24]={1024,989,887,724,512,265,0,-265,-512,-724,-887,-989,-1024,-989,-887,-724,-512,-265,0,265,512,724,887,989};
static const short S1[24]={0,265,512,724,887,989,1024,989,887,724,512,265,0,-265,-512,-724,-887,-989,-1024,-989,-887,-724,-512,-265};
static const short C4[24]={1024,512,-512,-1024,-512,512,1024,512,-512,-1024,-512,512,1024,512,-512,-1024,-512,512,1024,512,-512,-1024,-512,512};
static const short S4[24]={0,887,887,0,-887,-887,0,887,887,0,-887,-887,0,887,887,0,-887,-887,0,887,887,0,-887,-887};

static uint32_t isqrt64(uint64_t x)
{
    uint64_t r = 0u, b = 1ull << 62;
    while (b > x) b >>= 2;
    while (b) {
        if (x >= r + b) { x -= r + b; r = (r >> 1) + b; }
        else r >>= 1;
        b >>= 2;
    }
    return (uint32_t)r;
}

static uint32_t amp_permil(const uint32_t *d, const short *C, const short *S, uint32_t ssum)
{
        /* Under ~20 rpm re*re overflows int64 and fabricates an amplitude */
    if (ssum > 2900000u) return 0u;
    int64_t re = 0, im = 0;
    for (int n = 0; n < 24; n++) { re += (int64_t)d[n] * C[n]; im += (int64_t)d[n] * S[n]; }
    uint32_t mag = isqrt64((uint64_t)(re * re) + (uint64_t)(im * im));
    uint32_t num = (uint32_t)(((uint64_t)125u * mag) >> 6);
    return ssum ? num / ssum : 0u;
}

static void set_speed(uint16_t pwm_ticks)
{
    if (pwm_ticks > PWM_PERIOD_TICKS) pwm_ticks = PWM_PERIOD_TICKS;
        /* EN before SV on start, EN after SV on stop. Open drain: low = enabled */
    if (pwm_ticks) {
        GPIOA_BSRR = (1u << 24);               /* EN: sink low = enable       */
        TIM3_CCR1 = pwm_ticks;                 /* CH1 -> PB4 (D5) -> SV       */
        GPIOA_BSRR = (1u << 5);                /* LED on                      */
    } else {
        TIM3_CCR1 = 0u;                        /* SV to zero first            */
        GPIOA_BSRR = (1u << 8);                /* EN: release = output off    */
        GPIOA_BSRR = (1u << 21);               /* LED off                     */
    }
}

static void adc_init(void)
{
    RCC_APB2ENR |= (1u << 8);
    ADC_CCR = (ADC_CCR & ~(3u << 16)) | (1u << 23);   /* PCLK2/2, VREFINT on */
    ADC1_CR1 = 0u; ADC1_CR2 = 0u;
        /* 480-cycle sampling, ~61.5 us per conversion at 8 MHz ADC clock */
    ADC1_SMPR2 = (7u << 0) | (7u << 3);               /* ch0+ch1: 480 cyc (long S&H) */
    ADC1_SMPR1 = (7u << 0) | (7u << 21);              /* ch10, ch17: 480 cyc */
    ADC1_SQR1 = 0u;
    ADC1_CR2 |= 1u;
    uint32_t t0 = now_us(); while ((now_us() - t0) < 20u) { }
}

/* Sentinel: a conversion that timed out. MUST NOT be fed to a filter as if it
 * were a real 0-count sample (that would drag the average toward 0 V). */
#define ADC_FAIL 0xFFFFFFFFu

static uint32_t adc_read_channel(uint32_t channel)
{
    ADC1_SQR3 = channel & 0x1Fu;
    ADC1_SR = 0u;
    ADC1_CR2 |= (1u << 30);
    uint32_t t0 = now_us();
    while (!(ADC1_SR & (1u << 1))) { if ((now_us() - t0) > 200u) return ADC_FAIL; }
    return ADC1_DR & 0x0FFFu;
}
/* mV via this MCU's factory VREFIN_CAL:
 *   VDDA_mV = 3300 * VREFIN_CAL / ADCvrefint,  Vin_mV = ADCin * VDDA_mV / 4095
 * Diagnostics only; calibration uses the transmitted raw sums. */
static uint32_t adc_q8_mv(uint32_t q8, uint32_t vref_q8,
                          uint32_t vref_factory_cal_raw)
{
    if (!vref_q8 || !vref_factory_cal_raw) return 0u;
    uint64_t num = (uint64_t)q8 * VREFIN_CAL_VDDA_MV
                 * vref_factory_cal_raw;
    uint64_t den = (uint64_t)vref_q8 * 4095u;
    return (uint32_t)((num + den / 2u) / den);
}

static uint32_t adc_raw_mv(uint32_t raw, uint32_t vref_q8,
                           uint32_t vref_factory_cal_raw)
{
    return adc_q8_mv(raw << 8, vref_q8, vref_factory_cal_raw);
}

static uint32_t adc_mv(uint32_t ch, uint32_t vref_q8,
                       uint32_t vref_factory_cal_raw)
{
    uint32_t r = adc_read_channel(ch);
    return (r == ADC_FAIL) ? 0u
                           : adc_raw_mv(r, vref_q8, vref_factory_cal_raw);
}

static int ap_u(char *b, int p, uint32_t v)
{
    char tmp[10]; int n = 0;
    if (v == 0u) { b[p++] = '0'; return p; }
    while (v) { tmp[n++] = (char)('0' + (v % 10u)); v /= 10u; }
    while (n) { b[p++] = tmp[--n]; }
    return p;
}

static int ap_i(char *b, int p, int32_t v)
{
    if (v < 0) { b[p++] = '-'; v = -v; }
    return ap_u(b, p, (uint32_t)v);
}

static int ap_u64(char *b, int p, uint64_t v)
{
    char tmp[20]; int n = 0;
    if (v == 0u) { b[p++] = '0'; return p; }
    while (v) { tmp[n++] = (char)('0' + (v % 10u)); v /= 10u; }
    while (n) { b[p++] = tmp[--n]; }
    return p;
}

/* ================= KX134: bit-bang I2C on PB8=SCL, PB9=SDA =================
 * Proven on this exact board by the X-ray diagnostic (WHO_AM_I 0x46 @ 0x1F).
 * Open-drain emulation: drive low = output-low, release = input (ext pull-up). */
static uint8_t kx_addr = 0u, kx_who = 0u;
#define BB_SCL 8u
#define BB_SDA 9u

static void bb_delay(void) { uint32_t t0 = now_us(); while ((now_us() - t0) < 5u) { } }
static void bb_release(uint32_t pin) { GPIOB_MODER &= ~(3u << (pin * 2u)); }
static void bb_low(uint32_t pin)
{
    GPIOB_BSRR = (1u << (pin + 16u));
    GPIOB_MODER = (GPIOB_MODER & ~(3u << (pin * 2u))) | (1u << (pin * 2u));
}
static uint32_t bb_rd(uint32_t pin) { return (GPIOB_IDR >> pin) & 1u; }

static void bb_start(void)
{
    bb_release(BB_SDA); bb_release(BB_SCL); bb_delay();
    bb_low(BB_SDA); bb_delay(); bb_low(BB_SCL); bb_delay();
}
static void bb_stop(void)
{
    bb_low(BB_SDA); bb_delay(); bb_release(BB_SCL); bb_delay();
    bb_release(BB_SDA); bb_delay();
}
static int bb_wbyte(uint8_t b)
{
    for (int k = 7; k >= 0; k--) {
        if ((b >> k) & 1u) bb_release(BB_SDA); else bb_low(BB_SDA);
        bb_delay(); bb_release(BB_SCL); bb_delay(); bb_low(BB_SCL); bb_delay();
    }
    bb_release(BB_SDA); bb_delay(); bb_release(BB_SCL); bb_delay();
    uint32_t ack = !bb_rd(BB_SDA);
    bb_low(BB_SCL); bb_delay();
    return (int)ack;
}
static uint8_t bb_rbyte(int last)
{
    uint8_t v = 0u;
    bb_release(BB_SDA);
    for (int k = 7; k >= 0; k--) {
        bb_delay(); bb_release(BB_SCL); bb_delay();
        v = (uint8_t)((v << 1) | bb_rd(BB_SDA));
        bb_low(BB_SCL);
    }
    if (last) bb_release(BB_SDA); else bb_low(BB_SDA);   /* NACK / ACK */
    bb_delay(); bb_release(BB_SCL); bb_delay(); bb_low(BB_SCL); bb_delay();
    bb_release(BB_SDA);
    return v;
}

static int kx_read(uint8_t reg, uint8_t *buf, int n)
{
    bb_start();
    if (!bb_wbyte((uint8_t)(kx_addr << 1))) { bb_stop(); return -1; }
    if (!bb_wbyte(reg))                     { bb_stop(); return -2; }
    bb_start();
    if (!bb_wbyte((uint8_t)((kx_addr << 1) | 1u))) { bb_stop(); return -3; }
    for (int i = 0; i < n; i++) buf[i] = bb_rbyte(i == n - 1);
    bb_stop();
    return 0;
}
static int kx_write(uint8_t reg, uint8_t val)
{
    bb_start();
    if (!bb_wbyte((uint8_t)(kx_addr << 1))) { bb_stop(); return -1; }
    int ok = bb_wbyte(reg) && bb_wbyte(val);
    bb_stop();
    return ok ? 0 : -2;
}

static void kx134_init(void)
{
    /* pull-ups assist the breakout's own; recover any stuck slave */
    GPIOB_PUPDR = (GPIOB_PUPDR & ~((3u << 16) | (3u << 18))) | (1u << 16) | (1u << 18);
    GPIOB_OTYPER |= (1u << BB_SCL) | (1u << BB_SDA);
    bb_release(BB_SCL); bb_release(BB_SDA);
    { uint32_t t0 = now_us(); while ((now_us() - t0) < 500u) { } }
    for (int i = 0; i < 9; i++) { bb_low(BB_SCL); bb_delay(); bb_release(BB_SCL); bb_delay(); }
    bb_stop();

    const uint8_t cand[2] = { 0x1Fu, 0x1Eu };          /* 0x1F found on this bench */
    for (int i = 0; i < 2 && !kx_addr; i++) {
        kx_addr = cand[i];
        uint8_t v = 0u;
        if (kx_read(0x13u, &v, 1) == 0 && (v == 0x46u || v == 0x3Du)) { kx_who = v; break; }
        kx_addr = 0u;
    }
    if (kx_addr) {
        kx_write(0x1Bu, 0x00u);   /* CNTL1: standby                 */
        kx_write(0x21u, 0x0Du);   /* ODCNTL: 1600 Hz                */
        kx_write(0x1Bu, 0xC8u);   /* CNTL1: operate, RES, +/-16 g   */
    }
    g_tlm[T_KXADDR] = kx_addr; g_tlm[T_KXWHO] = kx_who;
}

static int kx_read_xyz(int16_t *x, int16_t *y, int16_t *z)
{
    uint8_t d[6];
    if (!kx_addr) return -1;
    if (kx_read(0x08u, d, 6)) return -2;
    *x = (int16_t)((uint16_t)d[0] | ((uint16_t)d[1] << 8));
    *y = (int16_t)((uint16_t)d[2] | ((uint16_t)d[3] << 8));
    *z = (int16_t)((uint16_t)d[4] | ((uint16_t)d[5] << 8));
    return 0;
}

/* =============================== main =============================== */
int main(void)
{
    RCC_AHB1ENR |= 7u;                                   /* GPIOA+B+C */
    RCC_APB1ENR |= (1u << 0) | (1u << 1) | (1u << 17);   /* TIM2+TIM3+USART2 */
    RCC_APB2ENR |= (1u << 14);                           /* SYSCFG */

    /* PA2/PA3 = USART2 (AF7); PA5 = LED out. (PA7/D11 unused now.) */
    GPIOA_MODER = (GPIOA_MODER & ~((3u << 4) | (3u << 6) | (3u << 10)))
                | (2u << 4) | (2u << 6) | (1u << 10);
    GPIOA_AFRL  = (GPIOA_AFRL & ~((0xFu << 8) | (0xFu << 12)))
                | (7u << 8) | (7u << 12);

    /* PB4 (D5) = TIM3_CH1 (AF2) PWM -> driver SV. (PB4 defaults to NJTRST;
     * the AF assignment overrides it. SWD debug uses PA13/PA14 only.) */
    GPIOB_MODER = (GPIOB_MODER & ~(3u << 8)) | (2u << 8);
    GPIOB_AFRL  = (GPIOB_AFRL & ~(0xFu << 16)) | (2u << 16);

        /* EN released before the pin becomes an output, so the mode switch
         * cannot glitch the driver enabled */
    GPIOA_BSRR   = (1u << 8);
    GPIOA_OTYPER |= (1u << 8);
    GPIOA_PUPDR &= ~(3u << 16);
    GPIOA_MODER  = (GPIOA_MODER & ~(3u << 16)) | (1u << 16);

    /* Halls: PA10, PB3, PB5 inputs with pull-ups */
    GPIOA_MODER &= ~(3u << 20);
    GPIOA_PUPDR  = (GPIOA_PUPDR & ~(3u << 20)) | (1u << 20);
    GPIOB_MODER &= ~((3u << 6) | (3u << 10));
    GPIOB_PUPDR  = (GPIOB_PUPDR & ~((3u << 6) | (3u << 10))) | (1u << 6) | (1u << 10);

    /* PA0 (current) + PA1 (NTC) analog; PC0 legacy diag analog */
    GPIOA_MODER |= (3u << 0) | (3u << 2);
    GPIOA_PUPDR &= ~((3u << 0) | (3u << 2));
    GPIOC_MODER |= (3u << 0);
    GPIOC_PUPDR &= ~(3u << 0);

    /* Button PC13, pull-up (active low) */
    GPIOC_MODER &= ~(3u << 26);
    GPIOC_PUPDR  = (GPIOC_PUPDR & ~(3u << 26)) | (1u << 26);

    /* TIM2 1 us free-run */
    TIM2_PSC = 15u; TIM2_ARR = 0xFFFFFFFFu; TIM2_EGR = 1u; TIM2_CR1 = 1u;

    /* USART2 115200 8N1, TX + RX with RX interrupt */
    USART2_CR1 = 0u;
    USART2_BRR = 139u;
    USART2_CR1 = (1u << 13) | (1u << 3) | (1u << 2) | (1u << 5); /* UE TE RE RXNEIE */

        /* USART1 TX-only to the Pi, one way, 115200 */
    RCC_APB2ENR |= (1u << 4);
    GPIOA_MODER = (GPIOA_MODER & ~(3u << 18)) | (2u << 18);   /* PA9 AF     */
    GPIOA_AFRH  = (GPIOA_AFRH & ~(0xFu << 4)) | (7u << 4);    /* AF7 USART1 */
    USART1_CR1 = 0u;
    USART1_BRR = 139u;
    USART1_CR1 = (1u << 13) | (1u << 3);                      /* UE TE      */

    /* EXTI: line 3 -> PB3, line 5 -> PB5, line 10 -> PA10, both edges */
    SYSCFG_EXTICR1 = (SYSCFG_EXTICR1 & ~(0xFu << 12)) | (1u << 12); /* EXTI3 = PB */
    SYSCFG_EXTICR2 = (SYSCFG_EXTICR2 & ~(0xFu << 4))  | (1u << 4);  /* EXTI5 = PB */
    SYSCFG_EXTICR3 = (SYSCFG_EXTICR3 & ~(0xFu << 8));               /* EXTI10 = PA */
    EXTI_IMR  |= (1u << 3) | (1u << 5) | (1u << 10);
    EXTI_RTSR |= (1u << 3) | (1u << 5) | (1u << 10);
    EXTI_FTSR |= (1u << 3) | (1u << 5) | (1u << 10);
    NVIC_ISER0 = (1u << 9) | (1u << 23);      /* EXTI3, EXTI9_5   */
    NVIC_ISER1 = (1u << (38 - 32)) | (1u << (40 - 32)); /* USART2, EXTI15_10 */

    adc_init();

    /* TIM3_CH1 PWM 1 kHz on PB4 */
    TIM3_PSC = 15u; TIM3_ARR = PWM_PERIOD_TICKS - 1u; TIM3_CCR1 = 0u;
    TIM3_CCMR1 = (6u << 4) | (1u << 3);      /* OC1M=PWM1, OC1PE */
    TIM3_CCER = (1u << 0);                    /* CC1E */
    TIM3_EGR = 1u; TIM3_CR1 = (1u << 7) | 1u;

    kx134_init();

    const uint16_t *table = SPEED_TABLE;
    uint32_t idx = 5u;                        /* boot state: OFF */
    set_speed(table[idx]);
    g_tlm[T_SPEEDIDX] = idx;

    uint32_t hall_tail = 0u;
    uint32_t last_state = hall_state_now();
    uint32_t last_edge  = now_us();
    uint32_t ema_us = 0u, win_sum = 0u, win_cnt = 0u;
    uint64_t win_sq = 0u;
    uint32_t ring[24];
    uint32_t win_min = 0xFFFFFFFFu, win_max = 0u;
    uint32_t edges = 0u, illegal = 0u;
    uint32_t btn_prev = 0u, btn_t = 0u;
    uint32_t coasting = 0u, coast_t0 = 0u;
    char txbuf[352]; int txlen = 0, txpos = 0;
    /* one-time boot banner: lets the bench verify WHICH build is
     * running (a silent MSD-flash failure once left stale code live) */
    { const char *bv = "FWV,10\r\n"; int bi;
      for (bi = 0; bv[bi]; bi++) {
          while (!(USART2_SR & (1u << 7))) {}
          USART2_DR = (uint8_t)bv[bi];
      } }
    char pibuf[128]; int pilen = 0, pipos = 0;
    uint32_t fault_code = FAULT_IDLE_NA;
    uint32_t ft_tick = 0u;
    uint32_t last_print = 0u;
    uint32_t rx_tail = 0u;
    char cmdbuf[16]; int cmdlen = 0;
    uint32_t last_adc = 0u, current_ema_q8 = 0u, temp_ema_q8 = 0u, adc_alt = 0u;
    uint32_t temp_mv = 0u, pc0_mv = 0u, vref_raw = 1u, vref_ema_q8 = 0u;
    const uint32_t vref_factory_cal_raw = (uint32_t)VREFIN_CAL_RAW;
    /* Exact, unfiltered PA0 conversion aggregates for the current 100 ms frame.
     * Calibration consumes these fields, never the guarded/filtered display path. */
    uint32_t current_raw_sum = 0u, current_raw_count = 0u;
    uint64_t current_raw_sumsq = 0u;
    uint32_t current_raw_min = 0xFFFFFFFFu, current_raw_max = 0u;
    uint32_t current_raw_window_start_us = 0u, current_raw_window_end_us = 0u;
    uint32_t current_raw_fail_count = 0u;
    uint32_t frame_seq = 0u;
    uint32_t last_accel = 0u, acc_n = 0u, acc_max = 0u, acc_sum = 0u;
    uint64_t acc_sq = 0u;
    uint32_t accel_rms_mg = 0u, accel_pk_mg = 0u;
    uint32_t accel_rms_q8 = 0u, accel_pk_q8 = 0u;
        /* Gravity vector, EMA tau ~160 ms. Tilting moves it; |vector| stays 1 g */
    int32_t ax_dc_q8 = 0, ay_dc_q8 = 0, az_dc_q8 = 0;
    /* self-verification state */
    uint32_t cur_s0 = 0u, cur_s1 = 0u, cur_s2 = 0u, cur_n = 0u;  /* median-3 window */
    uint32_t cur_peak_raw = 0u;   /* max de-glitched sample since the last frame */
    uint32_t cur_min_raw  = 0xFFFFFFFFu; /* min de-glitched sample -> full envelope */
    uint32_t hall_seen = 0u;        /* bitmask of Hall codes 1..6 observed        */
    uint32_t acc_mean_last = 0u;    /* |accel| block mean, must be ~1 g           */
    uint32_t accel_read_ok = 0u;    /* last KX134 transfer succeeded              */

    for (;;) {
        uint32_t t = now_us();

            /* Fixed 1 kHz schedule. Advancing by one period, not to t, keeps the
             * filter tau at 256 ms when the blocking accel read stalls the loop */
        if ((uint32_t)(t - last_adc) >= 1000u) {
            last_adc += 1000u;                                   /* keep phase */
            if ((uint32_t)(t - last_adc) > 50000u) last_adc = t;  /* long stall: resync */
            /* ---- OVERSAMPLE the current channel across the chopper period ----
             * The BLD-510B chops at ~20 kHz and the PA0 RC (330R + 10nF) rolls
             * off at 48 kHz, so ripple can reach the ADC. A single 1 kHz sample
             * could repeatedly hit the same 20 kHz chopper phase. Eight maximum-
             * sample-time conversions span ~492 us, almost ten chopper periods.
             * Every untouched conversion is accumulated below for calibration;
             * median/EMA processing is a separate legacy display path. */
            uint32_t acc = 0u, got = 0u;
            for (uint32_t k = 0; k < 8u; k++) {
                uint32_t sample_start_us = now_us();
                uint32_t s = adc_read_channel(CH_CURRENT);
                uint32_t sample_end_us = now_us();
                if (s != ADC_FAIL) {
                    acc += s; got++;
                    if (current_raw_count == 0u)
                        current_raw_window_start_us = sample_start_us;
                    current_raw_window_end_us = sample_end_us;
                    current_raw_sum += s;
                    current_raw_sumsq += (uint64_t)s * s;
                    current_raw_count++;
                    if (s < current_raw_min) current_raw_min = s;
                    if (s > current_raw_max) current_raw_max = s;
                } else {
                    current_raw_fail_count++;
                }
            }
            uint32_t raw = got ? ((acc + got / 2u) / got) : ADC_FAIL;
                /* alpha 1/256 at 1 kHz, tau 256 ms. Timed-out conversions skipped */
            if (raw != ADC_FAIL) {
                cur_s2 = cur_s1; cur_s1 = cur_s0; cur_s0 = raw;
                if (cur_n < 3u) cur_n++;
                uint32_t f = (cur_n >= 3u) ? med3(cur_s0, cur_s1, cur_s2) : raw;
                    /* Peak hold: the EMA would hide a short transient */
                if (f > cur_peak_raw) cur_peak_raw = f;
                if (f < cur_min_raw)  cur_min_raw  = f;
                current_ema_q8 = current_ema_q8
                    ? current_ema_q8 - (current_ema_q8 >> 8) + f
                    : (f << 8);
            }
            adc_alt ^= 1u;
            if (adc_alt) {
                raw = adc_read_channel(CH_TEMP);
                /* alpha = 1/64 @ 500 Hz -> tau = 128 ms; thermal is slow */
                if (raw != ADC_FAIL) {
                    temp_ema_q8 = temp_ema_q8
                        ? temp_ema_q8 - (temp_ema_q8 >> 6) + (raw << 2)
                        : (raw << 8);
                }
            }
        }

        /* accelerometer @ 200 Hz, 32-sample RMS/peak blocks */
        if (kx_addr && (t - last_accel) >= 5000u) {
            last_accel = t;
            int16_t ax, ay, az;
            accel_read_ok = (kx_read_xyz(&ax, &ay, &az) == 0) ? 1u : 0u;
            if (accel_read_ok) {
                ax_dc_q8 += ((((int32_t)ax) << 8) - ax_dc_q8) >> 5;
                ay_dc_q8 += ((((int32_t)ay) << 8) - ay_dc_q8) >> 5;
                az_dc_q8 += ((((int32_t)az) << 8) - az_dc_q8) >> 5;
                uint32_t mag = isqrt64((uint64_t)((int64_t)ax * ax
                                     + (int64_t)ay * ay + (int64_t)az * az));
                acc_sum += mag; acc_sq += (uint64_t)mag * mag;
                if (mag > acc_max) acc_max = mag;
                if (++acc_n >= 32u) {
                        /* Exact integer variance. E[x^2]-(E[x])^2 floored both
                         * terms and injected ~22 mg of phantom RMS at rest */
                    uint64_t tot  = 32ull * acc_sq - (uint64_t)acc_sum * acc_sum;
                    uint32_t var  = (uint32_t)(tot >> 10);        /* / 32^2 */
                    /* gravity check: |(x,y,z)| is orientation-invariant, so a
                     * healthy KX134 reads ~1 g (2048 counts) no matter how the
                     * bracket is turned. Anything else means dead/frozen/mis-scaled. */
                    acc_mean_last = acc_sum >> 5;
                    uint32_t rms  = isqrt64(var) * 1000u / KX_COUNTS_PER_G;
                    /* peak referenced to the UNtruncated mean (acc_sum/32),
                     * scaled before dividing so it keeps sub-count resolution */
                    uint32_t pk   = (acc_max * 32u > acc_sum)
                                  ? ((acc_max * 32u - acc_sum) * 1000u) / (32u * KX_COUNTS_PER_G)
                                  : 0u;
                        /* Q8 EMA across blocks, tau ~0.6 s. Plain integers
                         * truncated both terms and froze under 4 mg */
                    accel_rms_q8 = accel_rms_q8
                        ? accel_rms_q8 - (accel_rms_q8 >> 2) + (rms << 6)
                        : (rms << 8);
                    accel_pk_q8  = accel_pk_q8
                        ? accel_pk_q8  - (accel_pk_q8  >> 2) + (pk  << 6)
                        : (pk  << 8);
                    accel_rms_mg = (accel_rms_q8 + 128u) >> 8;
                    accel_pk_mg  = (accel_pk_q8  + 128u) >> 8;
                    acc_sum = 0u; acc_sq = 0u; acc_max = 0u; acc_n = 0u;
                }
            }
        }

        /* drain Hall edge ring (filled by EXTI ISRs) */
        while (hall_tail != hall_head) {
            uint32_t et = hall_t[hall_tail & 63u];
            uint32_t s  = hall_s[hall_tail & 63u];
            hall_tail++;
            if (s == last_state) continue;
            if (s == 0u || s == 7u) {
                illegal++; g_tlm[T_ILLEGAL] = illegal; last_state = s;
                continue;
            }
            hall_seen |= (1u << (s - 1u));   /* proves each Hall line toggles:
                                              * all six codes can only appear if
                                              * all three sensors are alive */
            uint32_t dt = et - last_edge;
            if (dt < 100u) { continue; }   /* ringing/glitch: ignore entirely,
                                            * do NOT update last_state, or the
                                            * next real edge looks unchanged
                                            * and gets skipped (rpm reads 0) */
            last_edge = et; last_state = s; edges++;
            ema_us = ema_us ? (ema_us - (ema_us >> 3) + (dt >> 3)) : dt;
            if (dt < win_min) win_min = dt;
            if (dt > win_max) win_max = dt;
            ring[win_cnt] = dt;
            win_sum += dt;
            win_sq  += (uint64_t)dt * dt;
            if (++win_cnt >= 24u) {
                uint32_t avg = win_sum / 24u;
                g_tlm[T_RPM]    = win_sum ? (60000000u / win_sum) : 0u;
                g_tlm[T_AVG_US] = avg;
                g_tlm[T_MIN_US] = win_min;
                g_tlm[T_MAX_US] = win_max;
                g_tlm[T_RIPPLE] = avg ? ((win_max - win_min) * 1000u / avg) : 0u;
                uint64_t tot = 24ull * win_sq - (uint64_t)win_sum * win_sum;
                uint32_t var = (uint32_t)(tot >> 6) / 9u;
                uint32_t sd = isqrt64(var);
                g_tlm[T_PSTD]   = win_sum ? (1000u * 24u * sd) / win_sum : 0u;
                g_tlm[T_MECH1X] = amp_permil(ring, C1, S1, win_sum);
                g_tlm[T_ELEC]   = amp_permil(ring, C4, S4, win_sum);
                win_sum = 0u; win_sq = 0u; win_cnt = 0u;
                win_min = 0xFFFFFFFFu; win_max = 0u;
            }
            g_tlm[T_EDGES] = edges; g_tlm[T_STATE] = s;
        }

        /* ---- stopped detection (signed: ISR edge stamps can be newer than
         * the loop-top t, and unsigned subtraction would underflow huge) */
        if ((int32_t)(now_us() - last_edge) > 300000) {
            g_tlm[T_RPM] = 0u;
                /* Discard the partial window: it would straddle a stop */
            win_sum = 0u; win_sq = 0u; win_cnt = 0u;
            win_min = 0xFFFFFFFFu; win_max = 0u;
            if (coasting) { g_tlm[T_COAST_MS] = (last_edge - coast_t0) / 1000u; coasting = 0u; }
        }

        /* button (manual fallback) */
        uint32_t b = button();
        if (b && !btn_prev && (t - btn_t) > 200000u) {
            btn_t = t; idx = (idx + 1u) % 6u; set_speed(table[idx]);
            g_tlm[T_SPEEDIDX] = idx;
            if (table[idx] == 0u) { coast_t0 = t; g_tlm[T_RPM_ATCUT] = g_tlm[T_RPM]; coasting = 1u; }
            else { coasting = 0u; }
        }
        btn_prev = b;

        /* serial RX (drained from IRQ ring): "S<rpm>" */
        while (rx_tail != rx_head) {
            char c = (char)rx_ring[rx_tail & 63u];
            rx_tail++;
            if (c == '\r' || c == '\n') {
                if (cmdlen > 0 && (cmdbuf[0] == 'S' || cmdbuf[0] == 's')) {
                    uint32_t rpm = 0u;
                    for (int k = 1; k < cmdlen; k++)
                        if (cmdbuf[k] >= '0' && cmdbuf[k] <= '9')
                            rpm = rpm * 10u + (uint32_t)(cmdbuf[k] - '0');
                    /* 3.3V PWM into the driver's 5V-referenced SV scales duty
                     * by 3.3/5: effective full-scale = 3500*3.3/5 = 2310 rpm */
                    uint32_t pwm = (rpm * PWM_PERIOD_TICKS + 1155u) / 2310u;
                    if (pwm > PWM_PERIOD_TICKS) pwm = PWM_PERIOD_TICKS;
                    set_speed((uint16_t)pwm);
                    g_tlm[T_SPEEDIDX] = 9u;
                    if (rpm == 0u) { coast_t0 = t; g_tlm[T_RPM_ATCUT] = g_tlm[T_RPM]; coasting = 1u; }
                    else { coasting = 0u; }
                }
                cmdlen = 0;
            } else if (cmdlen < 15) {
                cmdbuf[cmdlen++] = c;
            }
        }

        /* non-blocking UART CSV stream (40 columns) */
        if (pipos < pilen) {
            if (USART1_SR & (1u << 7)) { USART1_DR = (uint8_t)pibuf[pipos++]; }
        }
        if (txpos < txlen) {
            if (USART2_SR & (1u << 7)) { USART2_DR = (uint8_t)txbuf[txpos++]; }
        } else if ((t - last_print) > 100000u) {
            last_print = t;
                /* Scaled from the Q8 accumulators, no truncate-then-scale */
            /* Eight untouched VREFINT conversions accompany every frame. Their
             * sum/count are transmitted verbatim so host analysis can normalize
             * PA0 codes without assuming 3.300 V or trusting firmware scaling. */
            uint32_t vref_raw_sum_frame = 0u, vref_raw_count_frame = 0u;
            uint32_t vref_raw_sumsq_frame = 0u;
            uint32_t vref_window_start_us = now_us();
            for (uint32_t k = 0u; k < 8u; k++) {
                uint32_t vr = adc_read_channel(CH_VREFINT);
                if (vr != ADC_FAIL) {
                    vref_raw_sum_frame += vr;
                    vref_raw_sumsq_frame += vr * vr;
                    vref_raw_count_frame++;
                }
            }
            uint32_t vref_window_end_us = now_us();
            if (vref_raw_count_frame) {
                uint32_t vf_q8 = ((vref_raw_sum_frame << 8)
                                + vref_raw_count_frame / 2u)
                               / vref_raw_count_frame;
                if (!vref_ema_q8) {
                    vref_ema_q8 = vf_q8;
                } else {
                    int32_t vd = (int32_t)vf_q8 - (int32_t)vref_ema_q8;
                    vref_ema_q8 = (uint32_t)((int32_t)vref_ema_q8 + vd / 16);
                }
                vref_raw = (vf_q8 + 128u) >> 8;
            }

                /* Raw PA0 window: every conversion, before any filtering */
            uint32_t i_raw_sum_frame = current_raw_sum;
            uint32_t i_raw_count_frame = current_raw_count;
            uint32_t i_raw_min_frame = (current_raw_min == 0xFFFFFFFFu)
                                     ? 0u : current_raw_min;
            uint32_t i_raw_max_frame = current_raw_max;
            uint64_t i_raw_sumsq_frame = current_raw_sumsq;
            uint32_t i_raw_window_start_frame = current_raw_window_start_us;
            uint32_t i_raw_window_end_frame = current_raw_window_end_us;
            uint32_t i_raw_fail_count_frame = current_raw_fail_count;
            current_raw_sum = 0u; current_raw_count = 0u;
            current_raw_sumsq = 0u;
            current_raw_min = 0xFFFFFFFFu; current_raw_max = 0u;
            current_raw_window_start_us = 0u; current_raw_window_end_us = 0u;
            current_raw_fail_count = 0u;

            temp_mv  = adc_q8_mv(temp_ema_q8, vref_ema_q8,
                                 vref_factory_cal_raw);
            pc0_mv   = adc_mv(CH_PC0, vref_ema_q8, vref_factory_cal_raw);
            g_tlm[T_TEMPMV] = temp_mv;
            uint32_t current_mv = adc_q8_mv(current_ema_q8, vref_ema_q8,
                                             vref_factory_cal_raw);
            g_tlm[T_IDCMV] = current_mv;
            /* peak over the last 100 ms window, then re-arm */
            uint32_t current_peak_raw_frame = cur_peak_raw;
            uint32_t current_min_raw_frame = cur_min_raw;
            uint32_t current_pk_mv = adc_raw_mv(current_peak_raw_frame,
                                                 vref_ema_q8,
                                                 vref_factory_cal_raw);
            uint32_t current_mn_mv = (current_min_raw_frame == 0xFFFFFFFFu) ? current_mv
                                   : adc_raw_mv(current_min_raw_frame,
                                                vref_ema_q8,
                                                vref_factory_cal_raw);
            cur_peak_raw = 0u; cur_min_raw = 0xFFFFFFFFu;

            /* per-channel self-verification (see the ST_* notes above) */
            uint32_t status = 0u;
            if (vref_raw  >= VREF_RAW_MIN && vref_raw  <= VREF_RAW_MAX) status |= ST_ADC_OK;
            if (current_mv >= CURR_MV_MIN && current_mv <= CURR_MV_MAX) status |= ST_CURR_OK;
            if (temp_mv    >= TEMP_MV_MIN && temp_mv    <= TEMP_MV_MAX) status |= ST_TEMP_OK;
            if (kx_addr) status |= ST_KX_ID_OK;
            if (kx_addr && accel_read_ok
                && acc_mean_last >= ACC_1G_MIN && acc_mean_last <= ACC_1G_MAX) status |= ST_ACCEL_OK;
            {   /* Hall: current code legal, and every code reachable */
                uint32_t hs = g_tlm[T_STATE];
                if (hs >= 1u && hs <= 6u) status |= ST_HALL_OK;
                if ((hall_seen & 0x3Fu) == 0x3Fu) status |= ST_HALL_ALL6;
                status |= (hall_seen & 0x3Fu) << ST_HALL_SEEN_SHIFT;
            }
            if (g_tlm[T_RPM] > 0u) status |= ST_SPIN;
            g_tlm[T_STATUS] = status;

            int p = 0;
            p = ap_u(txbuf, p, t / 1000u);            txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_RPM]);         txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_RIPPLE]);      txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_MIN_US]);      txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_MAX_US]);      txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_STATE]);       txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_ILLEGAL]);     txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_EDGES]);       txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_SPEEDIDX]);    txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_COAST_MS]);    txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_PSTD]);        txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_MECH1X]);      txbuf[p++] = ',';
            p = ap_u(txbuf, p, g_tlm[T_ELEC]);        txbuf[p++] = ',';
            p = ap_u(txbuf, p, current_mv);           txbuf[p++] = ',';
            p = ap_u(txbuf, p, vref_raw);             txbuf[p++] = ',';
            p = ap_u(txbuf, p, pc0_mv);               txbuf[p++] = ',';
            p = ap_u(txbuf, p, accel_rms_mg);         txbuf[p++] = ',';
            p = ap_u(txbuf, p, accel_pk_mg);          txbuf[p++] = ',';
            p = ap_u(txbuf, p, temp_mv);              txbuf[p++] = ',';
            p = ap_u(txbuf, p, status);               txbuf[p++] = ',';
            p = ap_u(txbuf, p, current_pk_mv);        txbuf[p++] = ',';
            p = ap_u(txbuf, p, current_mn_mv);        txbuf[p++] = ',';
            p = ap_u(txbuf, p, i_raw_sum_frame);      txbuf[p++] = ',';
            p = ap_u(txbuf, p, i_raw_count_frame);    txbuf[p++] = ',';
            p = ap_u(txbuf, p, i_raw_min_frame);      txbuf[p++] = ',';
            p = ap_u(txbuf, p, i_raw_max_frame);      txbuf[p++] = ',';
            p = ap_u(txbuf, p, current_ema_q8);       txbuf[p++] = ',';
            p = ap_u(txbuf, p, vref_raw_sum_frame);   txbuf[p++] = ',';
            p = ap_u(txbuf, p, vref_raw_count_frame); txbuf[p++] = ',';
            p = ap_u(txbuf, p, vref_ema_q8);          txbuf[p++] = ',';
            p = ap_u(txbuf, p, vref_factory_cal_raw); txbuf[p++] = ',';
            p = ap_u(txbuf, p, frame_seq++);          txbuf[p++] = ',';
            p = ap_u(txbuf, p, TIM3_CCR1);            txbuf[p++] = ',';
            p = ap_u(txbuf, p, i_raw_window_start_frame); txbuf[p++] = ',';
            p = ap_u(txbuf, p, i_raw_window_end_frame);   txbuf[p++] = ',';
            p = ap_u(txbuf, p, i_raw_fail_count_frame);   txbuf[p++] = ',';
            p = ap_u(txbuf, p, vref_window_start_us);     txbuf[p++] = ',';
            p = ap_u(txbuf, p, vref_window_end_us);       txbuf[p++] = ',';
            p = ap_u64(txbuf, p, i_raw_sumsq_frame);      txbuf[p++] = ',';
            p = ap_u(txbuf, p, vref_raw_sumsq_frame);     txbuf[p++] = ',';
            /* orientation (gravity vector), signed milli-g per axis */
            p = ap_i(txbuf, p, (int32_t)(((int64_t)ax_dc_q8 * 1000) / (256 * KX_COUNTS_PER_G))); txbuf[p++] = ',';
            p = ap_i(txbuf, p, (int32_t)(((int64_t)ay_dc_q8 * 1000) / (256 * KX_COUNTS_PER_G))); txbuf[p++] = ',';
            p = ap_i(txbuf, p, (int32_t)(((int64_t)az_dc_q8 * 1000) / (256 * KX_COUNTS_PER_G))); txbuf[p++] = ',';
            p = ap_u(txbuf, p, fault_code);           txbuf[p++] = '\r'; txbuf[p++] = '\n';
            txlen = p; txpos = 0;

            /* on-device fault detector */
            {
                float mv_now = (float)current_mv;
                uint32_t rpm_now = g_tlm[T_RPM];
                float gx = (float)((int32_t)(((int64_t)ax_dc_q8 * 1000) / (256 * KX_COUNTS_PER_G)));
                float gy = (float)((int32_t)(((int64_t)ay_dc_q8 * 1000) / (256 * KX_COUNTS_PER_G)));
                float gz = (float)((int32_t)(((int64_t)az_dc_q8 * 1000) / (256 * KX_COUNTS_PER_G)));
                float *slot;
                if (TIM3_CCR1 == 0u && rpm_now == 0u) {
                    if (ft_anchor_settle < 1000u) ft_anchor_settle += 100u;
                    if (ft_anchor_settle >= 400u) {
                        ft_anchor_mv += (mv_now - ft_anchor_mv) * 0.05f;
                            /* Snap on first settled rest, then track at tau ~50 s */
                        if (ft_tbase_mv < 1.0f) ft_tbase_mv = (float)temp_mv;
                        else ft_tbase_mv += ((float)temp_mv - ft_tbase_mv) * 0.002f;
                    }
                } else {
                    ft_anchor_settle = 0u;
                }
                slot = ft_ring[ft_head];
                slot[0] = mv_now - ft_anchor_mv;
                slot[1] = (float)rpm_now;
                slot[2] = (float)g_tlm[T_RIPPLE];
                slot[3] = (float)g_tlm[T_MECH1X];
                slot[4] = (float)accel_rms_mg * FT_KG;
                slot[5] = (float)accel_pk_mg * FT_KG;
                slot[6] = (float)temp_mv;
                slot[7] = FT_G0X + (gx - FT_GBX) * FT_KG;
                slot[8] = FT_G0Y + (gy - FT_GBY) * FT_KG;
                slot[9] = FT_G0Z + (gz - FT_GBZ) * FT_KG;
                ft_head = (ft_head + 1) % FT_WIN;
                if (ft_n < FT_WIN) ft_n++;
                ft_tick++;
                    /* Hold 10 s after boot while the gravity filter converges */
                if (ft_n == FT_WIN && ft_tick >= 100u
                    && (ft_tick % 5u) == 0u) {
                    if (rpm_now < 150u) {
                        fault_code = FAULT_IDLE_NA;
                        ft_cand = FAULT_IDLE_NA; ft_streak = 0u;
                        ft_gprev = -1.0f;
                    } else {
                        uint32_t raw_verdict;
                        float f[16];
                        float s0=0,sq0=0,mx0=-1e9f,s1=0,sq1=0,s2=0,s3=0,s4=0,mx8=-1e9f,s6=0,s7=0,s8=0,s9=0,sg=0,sqg=0;
                        float t_first, t_last, n = (float)FT_WIN;
                        int k;
                        for (k = 0; k < FT_WIN; k++) {
                            float *w = ft_ring[k];
                            float dx = w[7]-FT_G0X, dy = w[8]-FT_G0Y, dz = w[9]-FT_G0Z;
                            float gd = ft_sqrtf(dx*dx + dy*dy + dz*dz);
                            s0+=w[0]; sq0+=w[0]*w[0]; if (w[0]>mx0) mx0=w[0];
                            s1+=w[1]; sq1+=w[1]*w[1];
                            s2+=w[2]; s3+=w[3]; s4+=w[4];
                            if (w[5]>mx8) mx8=w[5];
                            s6+=w[6]; s7+=w[7]; s8+=w[8]; s9+=w[9];
                            sg+=gd; sqg+=gd*gd;
                        }
                        t_last  = ft_ring[(ft_head + FT_WIN - 1) % FT_WIN][6];
                        t_first = ft_ring[ft_head][6];
                        {
                            float m0=s0/n, m1=s1/n, mg=sg/n;
                            float v0=sq0/n-m0*m0, v1=sq1/n-m1*m1, vg=sqg/n-mg*mg;
                            /* Catch-and-clamp negative variance from rounding. */
                            if (v0<0) v0=0;
                            if (v1<0) v1=0;
                            if (vg<0) vg=0;
                            f[0]=m0; f[1]=ft_sqrtf(v0); f[2]=mx0;
                            f[3]=m1; f[4]=ft_sqrtf(v1);
                            f[5]=s2/n; f[6]=s3/n;
                            f[7]=s4/n; f[8]=mx8;
                            f[9]=s6/n; f[10]=t_last-t_first;
                            f[11]=s7/n; f[12]=s8/n; f[13]=s9/n;
                            f[14]=mg; f[15]=ft_sqrtf(vg);
                        }
                            /* Rule ladder, generalized from the campaign so each
                             * fault is recognized at any speed:
                             *   DRAG      current well above idle, or rpm sag with it
                             *   OVERHEAT  temperature up >3.5 degC, current not drag
                             *   POSITION  orientation change >22 deg
                             *   LOOSE     small steady lean, ~9 deg
                             * No signature: the tree decides, but a fault claim must
                             * be backed by its own physics or it reads healthy.
                             * Validated by tools/validate_rules.py: 0 false fires on
                             * 7189 healthy windows, 57/57 runs correct. */
                        /* Ladder VALIDATED against all 57 campaign runs
                         * (tools/validate_rules.py): 0 false fires on 7189
                         * healthy windows, 57/57 runs correct. Margins:
                         * healthy worst dmv 13 / gdev 30 / gstd 15 / dT 12. */
                        {
                            float dT = (ft_tbase_mv > 1.0f)
                                       ? (ft_tbase_mv - f[9]) : 0.0f;
                            float exp_rpm = (float)TIM3_CCR1 * 2310.0f
                                            / (float)PWM_PERIOD_TICKS;
                            uint32_t tree_v = (uint32_t)fault_tree_classify(f);
                            if (f[0] > 30.0f || f[2] > 90.0f ||
                                (exp_rpm - f[3] > 250.0f && f[0] > 16.0f
                                 && f[4] < 100.0f)) {
                                raw_verdict = 2u;      /* ROTOR DRAG   */
                            } else if (dT > 120.0f) {
                                raw_verdict = 3u;      /* OVERHEAT     */
                            } else if (f[14] > 900.0f ||
                                       (f[14] > 220.0f && f[15] < 20.0f)) {
                                    /* Real tilt is large and steady; blasts are choppy */
                                raw_verdict = 4u;      /* MOTOR POSITION */
                            } else if (dT < 25.0f && f[0] < 20.0f
                                       && f[14] > 55.0f
                                       && (ft_gprev < 0.0f ||
                                           (f[14] - ft_gprev < 12.0f &&
                                            ft_gprev - f[14] < 12.0f))) {
                                    /* Loose is a steady lean with healthy current */
                                raw_verdict = 1u;      /* LOOSE MOUNT  */
                            } else {
                                raw_verdict = tree_v;
                                if ((tree_v == 2u && f[0] < 16.0f) ||
                                    (tree_v == 3u && dT < 80.0f) ||
                                    (tree_v == 4u && f[14] < 180.0f) ||
                                    (tree_v == 1u && !(f[0] < 20.0f
                                     && (f[14] >= 45.0f || f[8] >= 700.0f))))
                                    raw_verdict = 0u;  /* unbacked claim */
                            }
                            ft_gprev = f[14];
                        }
                        if (fault_code == 2u) ft_since_drag = 0u;
                        else if (ft_since_drag < 9999u) ft_since_drag++;
                        if (raw_verdict == fault_code) {
                            ft_cand = raw_verdict; ft_streak = 0u;
                        } else {
                                /* 3 s to latch, 6 s within 60 s of a drag report */
                            uint32_t need = (raw_verdict == 0u) ? 2u
                                          : (raw_verdict == 1u)
                                            ? ((ft_since_drag <= 120u) ? 12u : 6u)
                                            : 4u;
                            if (raw_verdict == ft_cand) ft_streak++;
                            else { ft_cand = raw_verdict; ft_streak = 1u; }
                            if (ft_streak >= need)
                                fault_code = raw_verdict;
                        }
                    }
                }
            }

            /* ---- compact Pi frame on USART1 (PA9/D8) ----------------------
             * PI,<seq>,<fault>,<rpm>,<i_mA>,<temp_mv>,<vrms>,<vpk>,<gx>,<gy>,<gz> */
            if (pipos >= pilen) {
                int q = 0;
                int32_t dmv_i = (int32_t)((float)current_mv - ft_anchor_mv);
                int32_t ma = (dmv_i * 1000) / 264 + 53;
                pibuf[q++]='P'; pibuf[q++]='I'; pibuf[q++]=',';
                q = ap_u(pibuf, q, frame_seq);            pibuf[q++] = ',';
                q = ap_u(pibuf, q, fault_code);           pibuf[q++] = ',';
                q = ap_u(pibuf, q, g_tlm[T_RPM]);         pibuf[q++] = ',';
                q = ap_i(pibuf, q, ma);                   pibuf[q++] = ',';
                q = ap_u(pibuf, q, temp_mv);              pibuf[q++] = ',';
                q = ap_u(pibuf, q, accel_rms_mg);         pibuf[q++] = ',';
                q = ap_u(pibuf, q, accel_pk_mg);          pibuf[q++] = ',';
                q = ap_i(pibuf, q, (int32_t)(((int64_t)ax_dc_q8 * 1000) / (256 * KX_COUNTS_PER_G))); pibuf[q++] = ',';
                q = ap_i(pibuf, q, (int32_t)(((int64_t)ay_dc_q8 * 1000) / (256 * KX_COUNTS_PER_G))); pibuf[q++] = ',';
                q = ap_i(pibuf, q, (int32_t)(((int64_t)az_dc_q8 * 1000) / (256 * KX_COUNTS_PER_G)));
                pibuf[q++] = '\r'; pibuf[q++] = '\n';
                pilen = q; pipos = 0;
            }
        }
    }
}

/* startup + full vector table */
extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;

void Reset_Handler(void)
{
    uint32_t *src = &_sidata, *dst = &_sdata;
    while (dst < &_edata) { *dst++ = *src++; }
    for (dst = &_sbss; dst < &_ebss;) { *dst++ = 0; }
    main();
    for (;;) { }
}

void Default_Handler(void) { for (;;) { } }

typedef void (*vec_t)(void);
__attribute__((section(".isr_vector"), used))
const vec_t vector_table[16 + 44] = {
    (vec_t)(&_estack),
    Reset_Handler,
    Default_Handler, Default_Handler, Default_Handler,   /* NMI HardFault MemManage */
    Default_Handler, Default_Handler,                    /* BusFault UsageFault     */
    0, 0, 0, 0,
    Default_Handler, Default_Handler,                    /* SVC DebugMon            */
    0,
    Default_Handler, Default_Handler,                    /* PendSV SysTick          */
    /* IRQ0..IRQ43 */
    [16 + 9]  = EXTI3_IRQHandler,
    [16 + 23] = EXTI9_5_IRQHandler,
    [16 + 38] = USART2_IRQHandler,
    [16 + 40] = EXTI15_10_IRQHandler,
};

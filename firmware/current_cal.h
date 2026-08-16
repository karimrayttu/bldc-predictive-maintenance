/*
 * Unit-specific ADC reference data for the STM32F401RE.
 *
 * ST factory-programs VREFIN_CAL at 0x1FFF7A2A. It is the 12-bit VREFINT
 * conversion measured for this exact MCU at VDDA = 3.3 V and 30 C. Using the
 * ROM value removes the unsupported hard-coded 1.209 V assumption while keeping
 * every transmitted PA0/VREFINT aggregate untouched and independently auditable.
 *
 * No current sensitivity, zero, idle current, or fitted coefficient is compiled
 * into the MCU. Current is not calculated until fresh series-ammeter points pass
 * the host calibration checks.
 */
#ifndef CURRENT_CAL_H
#define CURRENT_CAL_H

#define VREFIN_CAL_ADDR       0x1FFF7A2Au
#define VREFIN_CAL_VDDA_MV    3300u
#define VREFIN_CAL_RAW        (*(const volatile uint16_t *)VREFIN_CAL_ADDR)

#endif

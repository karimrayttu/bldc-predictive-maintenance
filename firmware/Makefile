# BLDC PHM data node - NUCLEO-F401RE (STM32F401RE, Cortex-M4)
#
#   make            build build/motor.elf, .bin and .hex
#   make size       section sizes
#   make flash      write build/motor.bin via STM32_Programmer_CLI
#   make clean
#
# Needs arm-none-eabi-gcc. STM32CubeCLT ships one:
#   C:/ST/STM32CubeCLT_1.21.0/GNU-tools-for-STM32/bin
#
# Bare metal: no HAL, no CMSIS, no startup.s. main.c carries its own vector
# table, so the link is -nostdlib plus libgcc for the soft-float and 64-bit
# division helpers the M4 has no instructions for.

TARGET  := motor
BUILD   := build

CC      := arm-none-eabi-gcc
OBJCOPY := arm-none-eabi-objcopy
SIZE    := arm-none-eabi-size

SRCS    := main.c fault_tree.c
LDSCRIPT := STM32F401RE.ld

CPUFLAGS := -mcpu=cortex-m4 -mthumb -mfloat-abi=soft
CFLAGS   := $(CPUFLAGS) -O2 -Wall -Wextra -ffreestanding -fno-builtin \
            -ffunction-sections -fdata-sections
LDFLAGS  := $(CPUFLAGS) -nostdlib -T $(LDSCRIPT) -Wl,--gc-sections \
            -Wl,-Map,$(BUILD)/$(TARGET).map
LDLIBS   := -lgcc

# STM32CubeCLT programmer. Override on the command line if yours differs.
PROGRAMMER ?= STM32_Programmer_CLI

.PHONY: all size flash clean

all: $(BUILD)/$(TARGET).bin $(BUILD)/$(TARGET).hex size

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/$(TARGET).elf: $(SRCS) $(LDSCRIPT) | $(BUILD)
	$(CC) $(CFLAGS) $(SRCS) $(LDFLAGS) -o $@ $(LDLIBS)

$(BUILD)/$(TARGET).bin: $(BUILD)/$(TARGET).elf
	$(OBJCOPY) -O binary $< $@

$(BUILD)/$(TARGET).hex: $(BUILD)/$(TARGET).elf
	$(OBJCOPY) -O ihex $< $@

size: $(BUILD)/$(TARGET).elf
	$(SIZE) $<

# 0x08000000 is the start of flash on the F401RE.
flash: $(BUILD)/$(TARGET).bin
	$(PROGRAMMER) -c port=SWD -w $< 0x08000000 -v -rst

clean:
	rm -rf $(BUILD)

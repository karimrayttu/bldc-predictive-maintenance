# Diagrams

Two figures describe the system: what the hardware is, and what the firmware does with it.

| Render | Source (`src/`) | What it shows |
|---|---|---|
| `system_block_diagram.png` | `system_block.drawio` | Supply → driver → motor, the four sensing chains and their pin assignments, the MCU, and the display node. Shaded blocks are commercial hardware; white blocks are circuitry built for this project. |
| `firmware_flowchart.png` | `firmware_flowchart.drawio` | The 10 Hz firmware loop: sample, convert, transmit, then twice a second the feature/limit/tree/agreement/persistence chain that produces a fault code. |

`src/system_block_earlier_layout.drawio` is an earlier, wider layout of the same system diagram, kept
because it carries a few annotations the final version dropped.

Sources are [diagrams.net / draw.io](https://www.drawio.com/) XML; open them directly in the
desktop app or at app.diagrams.net and export with File → Export as → PNG.

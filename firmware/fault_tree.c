/* MCU-PROXY FAULT TREE: committed artifact, NOT reproducible from this repo.
 *
 * PROVENANCE (read this before trusting the file):
 *   This tree was emitted by an MCU-proxy training pass that is NOT committed
 *   here. tools/train_model.py writes model/fault_tree.c and
 *   model/fault_tree_nogravity.c only; nothing in this repository regenerates
 *   this file, model/features_mcu.json or model/fault_model_mcu.joblib. The
 *   quoted figures below (depth-7, 33 nodes, held-out window accuracy 0.9992)
 *   come from that vanished run and cannot be re-derived by a reader.
 *   Treat them as an undocumented claim, not a verified result.
 *
 * LEAKAGE: the root split is f[14] (gdev_mean), a gravity-deviation feature.
 *   28 of the 30 classifier-valid healthy runs predate per-axis gravity
 *   streaming, so their gravity is imputed while every fault run's is
 *   measured. This tree therefore keys partly on WHEN a run was recorded.
 *   See README.md, "Why there are two numbers". The rule ladder in main.c
 *   requires physical evidence before it accepts any fault verdict from this
 *   tree, which is what keeps that leak from reaching the LED on its own.
 *
 * DO NOT swap in model/fault_tree.c or model/fault_tree_nogravity.c: all three
 *   define the same fault_tree_classify() behind the same header but expect
 *   DIFFERENT feature vectors in DIFFERENT units (this one raw mV/mg from the
 *   ADC and accelerometer, those two physical A/C from the Python pipeline).
 *   Substituting one compiles and links silently and classifies garbage.
 *   FAULT_TREE_FEATURE_SET below is the guard: main.c checks it.
 *
 * f[16] order: dmv_mean,dmv_std,dmv_max,rpm_mean,rpm_std,ripple_mean,
 *   mech1x_mean,vib_rms_mean,vib_pk_max,temp_mv_mean,temp_mv_slope,
 *   gx_mean,gy_mean,gz_mean,gdev_mean,gdev_std   (raw mV / mg / permil)
 * returns 0=healthy 1=loose_mount 2=rotor_drag 3=overheat 4=orientation_change */
#include "fault_tree.h"

int fault_tree_classify(const float *f)
{
    if (f[14] <= 1065.5589f) { /* gdev_mean */
        if (f[14] <= 26.0568f) { /* gdev_mean */
            if (f[7] <= 77.5000f) { /* vib_rms_mean */
                if (f[14] <= 3.5412f) { /* gdev_mean */
                    return 0; /* healthy */
                } else {
                    return 0; /* healthy */
                }
            } else {
                return 2; /* rotor_drag */
            }
        } else {
            if (f[6] <= 0.3500f) { /* mech1x_mean */
                if (f[14] <= 65.3687f) { /* gdev_mean */
                    return 2; /* rotor_drag */
                } else {
                    if (f[8] <= 299.0000f) { /* vib_pk_max */
                        if (f[5] <= 304.3500f) { /* ripple_mean */
                            return 2; /* rotor_drag */
                        } else {
                            if (f[14] <= 66.5791f) { /* gdev_mean */
                                return 3; /* overheat */
                            } else {
                                return 3; /* overheat */
                            }
                        }
                    } else {
                        return 2; /* rotor_drag */
                    }
                }
            } else {
                if (f[12] <= 84.0500f) { /* gy_mean */
                    if (f[12] <= 83.0000f) { /* gy_mean */
                        if (f[6] <= 0.7500f) { /* mech1x_mean */
                            if (f[9] <= 1534.5000f) { /* temp_mv_mean */
                                return 2; /* rotor_drag */
                            } else {
                                return 0; /* healthy */
                            }
                        } else {
                            return 2; /* rotor_drag */
                        }
                    } else {
                        if (f[0] <= 7.6500f) { /* dmv_mean */
                            return 1; /* loose_mount */
                        } else {
                            return 2; /* rotor_drag */
                        }
                    }
                } else {
                    if (f[8] <= 516.0000f) { /* vib_pk_max */
                        if (f[10] <= -23.0000f) { /* temp_mv_slope */
                            return 1; /* loose_mount */
                        } else {
                            return 1; /* loose_mount */
                        }
                    } else {
                        return 2; /* rotor_drag */
                    }
                }
            }
        }
    } else {
        return 4; /* orientation_change */
    }
}

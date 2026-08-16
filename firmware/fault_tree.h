#ifndef FAULT_TREE_H
#define FAULT_TREE_H

/* Which feature vector fault_tree_classify() below expects.
 *
 * Three different fault_tree.c files in this repo export the same symbol behind
 * this same header, and they are NOT interchangeable:
 *
 *   firmware/fault_tree.c              MCU proxy, raw mV / mg / permil (this one)
 *   model/fault_tree.c                 16 physical features, A / C / mg
 *   model/fault_tree_nogravity.c       11 physical features, A / C
 *
 * Dropping one of the model/ trees into firmware/ used to compile, link and
 * classify garbage in silence. main.c asserts on this macro so it now fails at
 * build time instead. If you deliberately swap the tree, port main.c's feature
 * assembly to the new units and update the macro in the same change.
 */
#define FAULT_TREE_FEATURE_SET_MCU_PROXY_RAW 1
#define FAULT_TREE_FEATURE_SET FAULT_TREE_FEATURE_SET_MCU_PROXY_RAW

int fault_tree_classify(const float *f);
#define FAULT_HEALTHY 0
#define FAULT_LOOSE_MOUNT 1
#define FAULT_ROTOR_DRAG 2
#define FAULT_OVERHEAT 3
#define FAULT_ORIENTATION 4
#endif

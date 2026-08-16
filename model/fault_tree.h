#ifndef FAULT_TREE_H
#define FAULT_TREE_H
int fault_tree_classify(const float *f);
#define FAULT_HEALTHY 0
#define FAULT_LOOSE_MOUNT 1
#define FAULT_ROTOR_DRAG 2
#define FAULT_OVERHEAT 3
#define FAULT_ORIENTATION 4
#endif

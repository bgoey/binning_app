/*
 * binning/monotonic_c.c
 *
 * C extension for the two performance-critical inner loops:
 *   1. equal_freq_bins  — build initial equal-frequency bins from sorted data
 *   2. make_monotonic   — greedy merge until event-rate sequence is monotone
 *
 * Build:
 *   pip install numpy
 *   python binning/setup.py build_ext --inplace
 *
 * Python usage (automatic fallback in algorithm.py if not built):
 *   from binning._monotonic import equal_freq_bins_c, make_monotonic_c
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

/* ──────────────────────────────────────────────────────────────────
 * Internal bin struct
 * ──────────────────────────────────────────────────────────────────*/
typedef struct {
    double  min_val;
    double  max_val;
    int64_t n;
    int64_t events;
} Bin;

static double event_rate(const Bin *b) {
    return b->n > 0 ? (double)b->events / (double)b->n : 0.0;
}

/* Merge bin at index `i` into bin at index `i-1`, shift array left */
static void merge_into_prev(Bin *bins, int *len, int i) {
    bins[i-1].max_val  = bins[i].max_val;
    bins[i-1].n       += bins[i].n;
    bins[i-1].events  += bins[i].events;
    memmove(&bins[i], &bins[i+1], (*len - i - 1) * sizeof(Bin));
    (*len)--;
}

/* ──────────────────────────────────────────────────────────────────
 * equal_freq_bins_c(x, y, max_bins, min_pct)
 *
 * x, y  — Python lists of floats (already sorted by x)
 * Returns list of dicts {min, max, n, events}
 * ──────────────────────────────────────────────────────────────────*/
static PyObject *py_equal_freq_bins(PyObject *self, PyObject *args) {
    PyObject *x_list, *y_list;
    int       max_bins;
    double    min_pct;

    if (!PyArg_ParseTuple(args, "O!O!id",
                          &PyList_Type, &x_list,
                          &PyList_Type, &y_list,
                          &max_bins, &min_pct))
        return NULL;

    Py_ssize_t n = PyList_GET_SIZE(x_list);
    if (n == 0) return PyList_New(0);

    int64_t min_count = (int64_t)fmax(1.0, (double)n * min_pct / 100.0);
    int     n_bins    = (int)fmin((double)max_bins, (double)n / (double)min_count);
    if (n_bins < 1) n_bins = 1;

    int64_t bin_size = n / n_bins;

    Bin *bins = (Bin *)calloc(n_bins + 1, sizeof(Bin));
    if (!bins) return PyErr_NoMemory();

    int    nb = 0;
    int64_t i = 0;

    while (i < n) {
        int64_t end;
        if (nb == n_bins - 1)
            end = n;           /* last bin gets all remaining rows */
        else
            end = i + bin_size;

        Bin b = {0};
        b.min_val = PyFloat_AsDouble(PyList_GET_ITEM(x_list, i));
        b.max_val = PyFloat_AsDouble(PyList_GET_ITEM(x_list, end - 1));
        b.n       = end - i;

        for (int64_t j = i; j < end; j++) {
            double yv = PyFloat_AsDouble(PyList_GET_ITEM(y_list, j));
            if (yv > 0.5) b.events++;
        }

        bins[nb++] = b;
        i = end;
        if (i >= n) break;
    }

    /* Build Python list of dicts */
    PyObject *result = PyList_New(nb);
    for (int k = 0; k < nb; k++) {
        PyObject *d = PyDict_New();
        PyDict_SetItemString(d, "min",    PyFloat_FromDouble(bins[k].min_val));
        PyDict_SetItemString(d, "max",    PyFloat_FromDouble(bins[k].max_val));
        PyDict_SetItemString(d, "n",      PyLong_FromLongLong(bins[k].n));
        PyDict_SetItemString(d, "events", PyLong_FromLongLong(bins[k].events));
        PyList_SET_ITEM(result, k, d);
    }

    free(bins);
    return result;
}


/* ──────────────────────────────────────────────────────────────────
 * make_monotonic_c(bins)
 *
 * bins — Python list of dicts with keys: n, events
 * Returns (merged_bins_list, direction_str)
 *   direction_str is "inc" or "dec"
 * ──────────────────────────────────────────────────────────────────*/

/* Total IV helper (pure C, no Python calls) */
static double total_iv_c(const Bin *bins, int len,
                          int64_t te, int64_t tne,
                          double smoothing) {
    double iv = 0.0;
    for (int i = 0; i < len; i++) {
        int64_t ev = bins[i].events;
        int64_t ne = bins[i].n - ev;
        double dist_e  = fmax((double)ev, smoothing) / fmax((double)te,  1.0);
        double dist_ne = fmax((double)ne, smoothing) / fmax((double)tne, 1.0);
        if (dist_e > 0 && dist_ne > 0) {
            double woe = log(dist_e / dist_ne);
            iv += (dist_e - dist_ne) * woe;
        }
    }
    return iv;
}

/* One greedy monotonic pass.  direction: 1=increasing, -1=decreasing */
static int try_monotonic(const Bin *src, int src_len,
                          Bin *dst, int direction) {
    int len = src_len;
    memcpy(dst, src, src_len * sizeof(Bin));

    int changed = 1;
    while (changed && len > 2) {
        changed = 0;
        int    worst_i = -1;
        double worst_v = 0.0;

        for (int i = 1; i < len; i++) {
            double viol = direction == 1
                ? event_rate(&dst[i-1]) - event_rate(&dst[i])   /* inc: prev > next = bad */
                : event_rate(&dst[i])   - event_rate(&dst[i-1]);/* dec: next > prev = bad */
            if (viol > worst_v) {
                worst_v = viol;
                worst_i = i;
            }
        }
        if (worst_i > 0) {
            merge_into_prev(dst, &len, worst_i);
            changed = 1;
        }
    }
    return len;
}

static PyObject *py_make_monotonic(PyObject *self, PyObject *args) {
    PyObject *py_bins;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &py_bins))
        return NULL;

    int src_len = (int)PyList_GET_SIZE(py_bins);
    if (src_len == 0) return Py_BuildValue("(O,s)", py_bins, "inc");

    Bin *src = (Bin *)calloc(src_len, sizeof(Bin));
    if (!src) return PyErr_NoMemory();

    /* Unpack Python dicts → C structs */
    int64_t te = 0, tne = 0;
    for (int i = 0; i < src_len; i++) {
        PyObject *d = PyList_GET_ITEM(py_bins, i);
        src[i].min_val = PyFloat_AsDouble(PyDict_GetItemString(d, "min"));
        src[i].max_val = PyFloat_AsDouble(PyDict_GetItemString(d, "max"));
        src[i].n       = PyLong_AsLongLong(PyDict_GetItemString(d, "n"));
        src[i].events  = PyLong_AsLongLong(PyDict_GetItemString(d, "events"));
        te  += src[i].events;
        tne += src[i].n - src[i].events;
    }

    Bin *inc_bins = (Bin *)calloc(src_len, sizeof(Bin));
    Bin *dec_bins = (Bin *)calloc(src_len, sizeof(Bin));
    if (!inc_bins || !dec_bins) { free(src); free(inc_bins); free(dec_bins); return PyErr_NoMemory(); }

    int inc_len = try_monotonic(src, src_len, inc_bins,  1);
    int dec_len = try_monotonic(src, src_len, dec_bins, -1);
    free(src);

    double iv_inc = total_iv_c(inc_bins, inc_len, te, tne, 0.5);
    double iv_dec = total_iv_c(dec_bins, dec_len, te, tne, 0.5);

    Bin  *winner     = iv_inc >= iv_dec ? inc_bins : dec_bins;
    int   winner_len = iv_inc >= iv_dec ? inc_len  : dec_len;
    const char *dir  = iv_inc >= iv_dec ? "inc"    : "dec";

    /* Pack result back into Python list of dicts */
    PyObject *result = PyList_New(winner_len);
    for (int k = 0; k < winner_len; k++) {
        PyObject *d = PyDict_New();
        PyDict_SetItemString(d, "min",    PyFloat_FromDouble(winner[k].min_val));
        PyDict_SetItemString(d, "max",    PyFloat_FromDouble(winner[k].max_val));
        PyDict_SetItemString(d, "n",      PyLong_FromLongLong(winner[k].n));
        PyDict_SetItemString(d, "events", PyLong_FromLongLong(winner[k].events));
        PyList_SET_ITEM(result, k, d);
    }

    free(inc_bins);
    free(dec_bins);

    return Py_BuildValue("(O,s)", result, dir);
}


/* ──────────────────────────────────────────────────────────────────
 * Module definition
 * ──────────────────────────────────────────────────────────────────*/
static PyMethodDef methods[] = {
    {"equal_freq_bins_c", py_equal_freq_bins, METH_VARARGS,
     "equal_freq_bins_c(x, y, max_bins, min_pct) -> list[dict]\n"
     "Build equal-frequency bins from pre-sorted x, y lists."},
    {"make_monotonic_c",  py_make_monotonic,  METH_VARARGS,
     "make_monotonic_c(bins) -> (list[dict], direction)\n"
     "Greedy monotonic merge. Returns best-IV direction."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT, "_monotonic", NULL, -1, methods
};

PyMODINIT_FUNC PyInit__monotonic(void) {
    return PyModule_Create(&moduledef);
}

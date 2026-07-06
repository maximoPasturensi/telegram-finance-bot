import pytest

# simulamos la funcion de calculo de neto que usa /balance
def calcular_balance_neto(ingresos: float, gastos: float) -> float:
    return ingresos - gastos

# test 1 verificamos el calculo de numeros 
def test_balance_neto_positivo():
    resultado = calcular_balance_neto(5000.0, 1200.0)
    assert resultado == 3800.0

# test 2 verificar que maneje un balance negativo
def test_balance_neto_negativo():
    resultado = calcular_balance_neto(1000.0, 1500.0)
    assert resultado == -500.0

# test 3 verificar si no hay movimientos
def test_balance_cero():
    resultado = calcular_balance_neto(0.0, 0.0)
    assert  resultado == 0.0
"""Sinais de alarme (red flags) em gestantes que devem disparar escalada
imediata para a Dra. Leiza.

Esta lista é PROVISÓRIA — será revisada pela Dra. Leiza. O agente também
pode escalar por julgamento próprio ("não tenho certeza / caso ambíguo").

Mantenha cada item em linguagem simples (o que a paciente descreveria),
pois o texto entra direto no system prompt do agente.
"""

RED_FLAGS_GESTANTES: list[str] = [
    "Sangramento vaginal de qualquer intensidade",
    "Perda de líquido pela vagina (suspeita de bolsa rota)",
    "Dor abdominal forte ou contínua",
    "Redução ou ausência de movimentação fetal (após 24 semanas)",
    "Dor de cabeça forte que não passa com analgésico comum",
    "Alterações visuais: visão embaçada, pontos brilhantes, visão dupla",
    "Inchaço súbito em rosto, mãos ou pés",
    "Febre acima de 38°C",
    "Vômitos persistentes (mais de 24h sem conseguir hidratar)",
    "Falta de ar importante, dor no peito",
    "Suspeita de trabalho de parto antes de 37 semanas (contrações regulares)",
    "Desmaio ou tontura intensa",
    "Queimação ao urinar com febre ou dor lombar",
    "Pensamentos de se machucar ou de machucar o bebê",
    "Acidente, trauma ou queda envolvendo a barriga",
]

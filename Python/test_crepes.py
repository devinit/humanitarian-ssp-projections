import pandas as pd
from sklearn.model_selection import train_test_split
from crepes import WrapClassifier
from sklearn.ensemble import RandomForestClassifier


dataset = pd.read_csv('../intermediate_data/ssp1.csv')

y = dataset.pop('humanitarian').astype('str').astype('category').values
dataset.pop('humanitarian_needs')

X = dataset.values.astype(float)

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.5)

X_prop_train, X_cal, y_prop_train, y_cal = train_test_split(X_train, y_train, test_size=0.25)

rf = WrapClassifier(RandomForestClassifier(n_jobs=-1))

rf.fit(X_prop_train, y_prop_train)

rf.calibrate(X_cal, y_cal)

p_values = rf.predict_p(X_test)
predictions = rf.predict_set(X_test, confidence=0.9)
eval = rf.evaluate(X_test, y_test, confidence=0.9)

print(eval)
